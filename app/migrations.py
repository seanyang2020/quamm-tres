"""轻量数据迁移（幂等，SQLite/PG 通用）。

0.10.0 引入：principal 表新增 ``is_admin`` 布尔列，且仅 ``admin@corp.com`` 为管理员。
早期库可能缺该列（模型更早版本没有），也可能把 user01 误标成管理员，这里一并修正，
不删除任何已有记录。

接入真实 XOS 设备时：device 表补 project_name / board_dt / storage_type /
has_wifi / source 五列，并把既有行（全部是 mock 设备）统一标 source='mock'。

0.11.0 引入：credential 表补 role / note 两列（凭据自带权限档位），并把
admin@corp.com 的既有凭据回填为 role='admin'，保证「已有管理员」不被首次引导误判。
contacts / settings 为全新表，由 init_db() 的 create_all 负责建表。
只用 ALTER TABLE ADD COLUMN，不重建表、不丢任何已有记录。
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
}

# credential 表新增列（v0.11）-> DDL 片段
_CREDENTIAL_COLUMNS = {
    "role": "VARCHAR(16) NOT NULL DEFAULT 'user'",
    "note": "VARCHAR(256)",
}


def migrate(session: Session) -> None:
    """幂等迁移：补 device/credential 新列 + 补 is_admin 列 + 修正管理员标志 + 回填管理员 role。"""
    _ensure_device_columns(session)
    _ensure_columns(session, "credential", _CREDENTIAL_COLUMNS)
    _ensure_is_admin_column(session)
    _fix_admin_flags(session)
    _backfill_admin_role(session)


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
