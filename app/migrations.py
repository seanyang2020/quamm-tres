"""轻量数据迁移（幂等，SQLite/PG 通用）。

0.10.0 引入：principal 表新增 ``is_admin`` 布尔列，且仅 ``admin@corp.com`` 为管理员。
早期库可能缺该列（模型更早版本没有），也可能把 user01 误标成管理员，这里一并修正，
不删除任何已有记录。

接入真实 XOS 设备时：device 表补 project_name / board_dt / storage_type /
has_wifi / source 五列，并把既有行（全部是 mock 设备）统一标 source='mock'。

0.11.0 引入：credential 表补 role / note 两列（凭据自带权限档位），并把
admin@corp.com 的既有凭据回填为 role='admin'，保证「已有管理员」不被首次引导误判。
contacts / settings 为全新表，由 init_db() 的 create_all 负责建表。

0.12.0 引入：product_type 表补工程/编译元数据 6 列（defconfig / sdk_repo_cmd /
build_cmd / image_path / jenkins_url / notes），全部可空，允许「信息不全」机型存在。
只用 ALTER TABLE ADD COLUMN，不重建表、不丢任何已有记录。

0.14.0 引入：固件策略。product_type 补 db_project_name / required_major /
required_minor / required_patch，device 补 firmware_version；flash_job 为全新表
（create_all 建表）。db_project_name 按当前 5 个真实机型回填（仅空值回填，不覆盖人工修改）。
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.seed import ADMIN_EMAIL

# device 表新增列 -> DDL 片段（顺序无关，逐列判断是否存在）
_DEVICE_COLUMNS = {
    "project_name": "VARCHAR(128)",
    "board_dt": "VARCHAR(256)",
    "storage_type": "VARCHAR(16)",
    "has_wifi": "BOOLEAN",
    "source": "VARCHAR(16) NOT NULL DEFAULT 'mock'",
    "firmware_version": "VARCHAR(64)",
}

# credential 表新增列（v0.11）-> DDL 片段
_CREDENTIAL_COLUMNS = {
    "role": "VARCHAR(16) NOT NULL DEFAULT 'user'",
    "note": "VARCHAR(256)",
}

# product_type 表新增列（v0.12）-> DDL 片段（全部可空，缺任一即「信息不全」）
_PRODUCT_TYPE_COLUMNS = {
    "defconfig": "VARCHAR(255)",
    "sdk_repo_cmd": "VARCHAR(1024)",
    "build_cmd": "VARCHAR(1024)",
    "image_path": "VARCHAR(512)",
    "jenkins_url": "VARCHAR(512)",
    "notes": "TEXT",
}

# product_type 表新增列（v0.14）-> DDL 片段
_PRODUCT_TYPE_FIRMWARE_COLUMNS = {
    "db_project_name": "VARCHAR(128)",
    "required_major": "INTEGER NOT NULL DEFAULT 0",
    "required_minor": "INTEGER NOT NULL DEFAULT 0",
    "required_patch": "INTEGER NOT NULL DEFAULT 0",
}

# lease 表新增列（v0.18 心跳）-> DDL 片段
_LEASE_COLUMNS = {
    "last_heartbeat_at": "DATETIME",
}

# v0.14：当前 5 个真实机型的 DB 工程名（用 dbxostest.py -ls + 设备 /etc/fw_version
# 构建号对照确认）。只回填空值，不覆盖人工修改。
_DB_PROJECT_BACKFILL = {
    "esl2": "esl2",
    "framex": "framex",
    "moto_ld": "moto_ld_102d",
    "qm10xh_full": "10xh_sdk_demo_lh",
    "qm10xv_full": "XOS_SDK_Daily/10xv_lv",
}


def migrate(session: Session) -> None:
    """幂等迁移：补 device/credential/product_type/lease 新列 + 补 is_admin 列 + 修正管理员标志 + 回填管理员 role。"""
    _ensure_device_columns(session)
    _ensure_columns(session, "credential", _CREDENTIAL_COLUMNS)
    _ensure_columns(session, "product_type", _PRODUCT_TYPE_COLUMNS)
    _ensure_is_admin_column(session)
    _fix_admin_flags(session)
    _backfill_admin_role(session)
    _ensure_columns(session, "product_type", _PRODUCT_TYPE_FIRMWARE_COLUMNS)
    _backfill_db_project_names(session)
    _ensure_columns(session, "lease", _LEASE_COLUMNS)


def _ensure_columns(session: Session, table: str, ddl_map: dict[str, str]) -> None:
    """通用幂等 ALTER：逐列检查，缺哪列补哪列，已存在不报错。"""
    engine = session.get_bind()
    columns = {c["name"] for c in inspect(engine).get_columns(table)}
    added = []
    for name, ddl in ddl_map.items():
        if name not in columns:
            session.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
            added.append(name)
    if added:
        session.commit()


def _backfill_admin_role(session: Session) -> None:
    """把已有管理员的凭据标成 role='admin'（引导只在「无任何 admin token」时触发）。"""
    session.execute(
        text(
            "UPDATE credential SET role = 'admin' "
            "WHERE principal_id IN (SELECT id FROM principal WHERE email = :admin OR is_admin = 1)"
        ),
        {"admin": ADMIN_EMAIL},
    )
    session.commit()


def _backfill_db_project_names(session: Session) -> None:
    """v0.14：为空 db_project_name 的 5 个真实机型回填 DB 工程名（幂等）。"""
    for type_code, project in _DB_PROJECT_BACKFILL.items():
        session.execute(
            text(
                "UPDATE product_type SET db_project_name = :project "
                "WHERE type_code = :code AND (db_project_name IS NULL OR db_project_name = '')"
            ),
            {"project": project, "code": type_code},
        )
    session.commit()


def _ensure_device_columns(session: Session) -> None:
    engine = session.get_bind()
    columns = {c["name"] for c in inspect(engine).get_columns("device")}
    added = []
    for name, ddl in _DEVICE_COLUMNS.items():
        if name not in columns:
            session.execute(text(f"ALTER TABLE device ADD COLUMN {name} {ddl}"))
            added.append(name)
    if added:
        session.commit()
    # 既有 22 台 mock 设备：source 为空 / NULL 一律补成 mock
    session.execute(
        text("UPDATE device SET source = 'mock' WHERE source IS NULL OR source = ''")
    )
    session.commit()


def _ensure_is_admin_column(session: Session) -> None:
    engine = session.get_bind()
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("principal")}
    if "is_admin" not in columns:
        # SQLite ALTER TABLE ADD COLUMN（既有行默认 0 / false）
        session.execute(
            text("ALTER TABLE principal ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0")
        )
        session.commit()


def _fix_admin_flags(session: Session) -> None:
    session.execute(
        text("UPDATE principal SET is_admin = 0 WHERE email != :admin"),
        {"admin": ADMIN_EMAIL},
    )
    session.execute(
        text("UPDATE principal SET is_admin = 1 WHERE email = :admin"),
        {"admin": ADMIN_EMAIL},
    )
    session.commit()
