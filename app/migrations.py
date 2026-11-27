"""轻量数据迁移（幂等，SQLite/PG 通用）。

0.10.0 引入：principal 表新增 ``is_admin`` 布尔列，且仅 ``admin@corp.com`` 为管理员。
早期库可能缺该列（模型更早版本没有），也可能把 user01 误标成管理员，这里一并修正，
不删除任何已有记录。

接入真实 XOS 设备时：device 表补 project_name / board_dt / storage_type /
has_wifi / source 五列。source 新增列默认 real；不批量改写既有行的 source
（既有 mock 设备保留，由业务层 simulated 标记；空值不再刷成 mock）。

0.11.0 引入：credential 表补 role / note 两列（凭据自带权限档位），并把
admin@corp.com 的既有凭据回填为 role='admin'，保证「已有管理员」不被首次引导误判。
contacts / settings 为全新表，由 init_db() 的 create_all 负责建表。

0.12.0 引入：product_type 表补工程/编译元数据 6 列（defconfig / sdk_repo_cmd /
build_cmd / image_path / jenkins_url / notes），全部可空，允许「信息不全」机型存在。
只用 ALTER TABLE ADD COLUMN，不重建表、不丢任何已有记录。

0.14.0 引入：固件策略。product_type 补 db_project_name / required_major /
required_minor / required_patch，device 补 firmware_version；flash_job 为全新表
（create_all 建表）。db_project_name 按当前 5 个真实机型回填（仅空值回填，不覆盖人工修改）。

0.23.0 引入：芯片/浮点能力。product_type 补 chip_model / float_abi / has_fpu 三列
（inspect 判列后 ALTER ADD，幂等），默认 auto / 0，供设备截图选浮点 ABI。

0.24.0 引入：截图元数据表 screenshot_record 为全新表（create_all 自动建，无需
ALTER）。此处仅做幂等兜底：inspect 判表，缺表则补建；老库升级不丢任何数据。
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
    "source": "VARCHAR(16) NOT NULL DEFAULT 'real'",
    "firmware_version": "VARCHAR(64)",
    "register_agent": "VARCHAR(128)",
    # v0.31 工程配置指纹（JSON）
    "eng_config": "TEXT",
    # v0.26 共享范围与名单
    "share_scope": "VARCHAR(16) NOT NULL DEFAULT 'shared'",
    "allowed_emails": "TEXT",
    "allowed_agent_labels": "TEXT",
    # v0.27 手动锁定
    "locked": "BOOLEAN NOT NULL DEFAULT 0",
    "locked_by": "VARCHAR(128)",
    "locked_reason": "VARCHAR(128)",
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

# lease 表新增列（v0.30 租约级串口派生票）-> DDL 片段（inspect 判列后 ADD，幂等）
_LEASE_SERIAL_COLUMNS = {
    "serial_grant": "VARCHAR(16)",
    "serial_token_label": "VARCHAR(64)",
    "serial_grant_detail": "JSON",
    "serial_grant_at": "DATETIME",
}

# product_type 表新增列（v0.23 芯片/浮点能力）-> DDL 片段（inspect 判列后 ADD，幂等）
# float_abi 默认 auto（由探测推断），has_fpu 默认 0；不覆盖任何已有数据。
_PRODUCT_TYPE_CHIP_COLUMNS = {
    "chip_model": "VARCHAR(128)",
    "float_abi": "VARCHAR(16) DEFAULT 'auto'",
    "has_fpu": "BOOLEAN NOT NULL DEFAULT 0",
}

# product_type 表新增列（v0.25 编译服务器引用，可空）-> DDL 片段
_PRODUCT_TYPE_BUILD_COLUMNS = {
    "build_server_id": "VARCHAR(32)",
}

# wl_port_binding 表新增列（v0.32 TCP COM 资源接入）-> DDL 片段（inspect 判列后 ADD，幂等）
_WL_PORT_COLUMNS = {
    "endpoint": "VARCHAR(128)",
    "com_name": "VARCHAR(64)",
    "probe_json": "TEXT",
}

# wl_port_binding COMRes 扩展（§2.7）：唯一键 node_id+ip+tcp_port → com_key；占用续约字段
_WL_COMRES_COLUMNS = {
    "tcp_port": "INTEGER",
    "com_key": "VARCHAR(96)",
    "allocated_by": "VARCHAR(128)",
    "allocated_at": "DATETIME",
    "allocated_expire": "DATETIME",
}

# t969：TRES→endpoint 可达性（可空=未测；与节点自报 health_state 分口径）
_WL_REACH_COLUMNS = {
    "reach_ok": "BOOLEAN",
    "reach_error": "VARCHAR(64)",
    "reach_checked_at": "DATETIME",
}

# t981：standalone 设备指纹（fp.v1 JSON，可空）
_WL_FP_COLUMNS = {
    "fingerprint_json": "TEXT",
}

# wl_node：管理面可复制 node_token（Fernet 密文，t942）
_WL_NODE_COLUMNS = {
    "token_cipher": "TEXT",
}

# wl_client_apply：区分自助申请 / enroll 预置 token（t966）
_WL_CLIENT_APPLY_COLUMNS = {
    "source": "VARCHAR(16)",
}

# patrol_run 表新增列：区分全量巡检 / 待检+离线快轮询；旧行 DEFAULT full，不做回填推断
_PATROL_RUN_COLUMNS = {
    "mode": "VARCHAR(16) NOT NULL DEFAULT 'full'",
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
    _ensure_columns(session, "lease", _LEASE_SERIAL_COLUMNS)
    _ensure_columns(session, "product_type", _PRODUCT_TYPE_CHIP_COLUMNS)
    _ensure_screenshot_table(session)
    _ensure_columns(session, "product_type", _PRODUCT_TYPE_BUILD_COLUMNS)
    _rebuild_build_server_for_principal(session)
    _ensure_device_position_index(session)
    _ensure_columns(session, "wl_port_binding", _WL_PORT_COLUMNS)
    _ensure_columns(session, "wl_port_binding", _WL_COMRES_COLUMNS)
    _ensure_columns(session, "wl_port_binding", _WL_REACH_COLUMNS)
    _ensure_columns(session, "wl_port_binding", _WL_FP_COLUMNS)
    _ensure_columns(session, "wl_node", _WL_NODE_COLUMNS)
    _ensure_comres_unique_index(session)
    # t942：先清理历史重复，再补 (node_id, com_name) 唯一索引
    _dedupe_comres_bindings(session)
    _ensure_comres_node_com_unique_index(session)
    _backfill_wl_node_token_cipher(session)
    _ensure_columns(session, "patrol_run", _PATROL_RUN_COLUMNS)
    _ensure_wl_client_apply_table(session)
    _ensure_columns(session, "wl_client_apply", _WL_CLIENT_APPLY_COLUMNS)
    _ensure_wl_package_table(session)
    _ensure_powerres_tables(session)
    _ensure_columns(session, "power_res_inventory", _POWERRES_INV_COLUMNS)


# PT5a / PT8-tres：电源 inventory 补充列（幂等 ADD COLUMN）
_POWERRES_INV_COLUMNS = {
    "location": "VARCHAR(64)",
    "driver": "VARCHAR(32)",
    "restart_hold_ms": "INTEGER",
}


def _ensure_device_position_index(session: Session) -> None:
    """v0.29.2 幂等迁移：position_code 取消全库唯一，改普通索引。

    同一物理位置允许放多台设备。旧库遗留 ``uq_device_position_code`` 唯一索引时先 DROP，
    再补建非唯一索引 ``ix_device_position_code``；只动索引，不删除/修改任何设备数据。
    SQLite 与 PostgreSQL 均支持 ``DROP INDEX IF EXISTS``。
    """
    from sqlalchemy import Index

    from app.models import Device

    engine = session.get_bind()
    indexes = {idx["name"] for idx in inspect(engine).get_indexes("device")}
    if "uq_device_position_code" in indexes:
        session.execute(text("DROP INDEX IF EXISTS uq_device_position_code"))
        session.commit()
    if "ix_device_position_code" not in indexes:
        Index("ix_device_position_code", Device.position_code).create(bind=engine, checkfirst=True)


def _ensure_screenshot_table(session: Session) -> None:
    """v0.24 幂等兜底：screenshot_record 是全新表，create_all 已负责建表；
    这里用 inspect 判表，仅在缺表时补建（新表无需 ALTER、不涉及加列）。"""
    from app import models  # noqa: F401 确保 ScreenshotRecord 注册到 Base.metadata
    from app.db import Base

    engine = session.get_bind()
    if "screenshot_record" in inspect(engine).get_table_names():
        return
    Base.metadata.tables["screenshot_record"].create(bind=engine, checkfirst=True)


def _ensure_wl_client_apply_table(session: Session) -> None:
    """COMRes 客户端自助申请表：全新表，create_all 建；inspect 缺表则补建。"""
    from app import models  # noqa: F401
    from app.db import Base

    engine = session.get_bind()
    if "wl_client_apply" in inspect(engine).get_table_names():
        return
    Base.metadata.tables["wl_client_apply"].create(bind=engine, checkfirst=True)


def _ensure_wl_package_table(session: Session) -> None:
    """t976：客户端安装包表 wl_package；create_all 建，inspect 缺表则补建（幂等）。"""
    from app import models  # noqa: F401
    from app.db import Base

    engine = session.get_bind()
    if "wl_package" in inspect(engine).get_table_names():
        return
    Base.metadata.tables["wl_package"].create(bind=engine, checkfirst=True)


def _ensure_powerres_tables(session: Session) -> None:
    """PV6a：PowerRes 独立资源表；create_all 建，inspect 缺表则补建（幂等）。"""
    from app import models  # noqa: F401
    from app.db import Base

    engine = session.get_bind()
    names = set(inspect(engine).get_table_names())
    for table in ("power_res_binding", "power_res_inventory"):
        if table not in names and table in Base.metadata.tables:
            Base.metadata.tables[table].create(bind=engine, checkfirst=True)


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


def _ensure_comres_unique_index(session: Session) -> None:
    """COMRes：幂等补建 uq_wl_comres_key (node_id, com_key)。

    com_key 形如 ``{ip}:{tcp_port}``；SQLite/PG 上 NULL com_key 不参与唯一冲突，
    旧 wirelog 仅按 slot 的行可继续共存。
    """
    engine = session.get_bind()
    if "wl_port_binding" not in inspect(engine).get_table_names():
        return
    indexes = {idx["name"] for idx in inspect(engine).get_indexes("wl_port_binding")}
    if "uq_wl_comres_key" in indexes:
        return
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_wl_comres_key "
            "ON wl_port_binding(node_id, com_key)"
        )
    )
    session.commit()


def _ensure_comres_node_com_unique_index(session: Session) -> None:
    """t942：幂等补建 uq_wl_port_node_com (node_id, com_name)。"""
    engine = session.get_bind()
    if "wl_port_binding" not in inspect(engine).get_table_names():
        return
    indexes = {idx["name"] for idx in inspect(engine).get_indexes("wl_port_binding")}
    if "uq_wl_port_node_com" in indexes:
        return
    session.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_wl_port_node_com "
            "ON wl_port_binding(node_id, com_name)"
        )
    )
    session.commit()


def _dedupe_comres_bindings(session: Session) -> None:
    """t942 一次性迁移（幂等）：按 (node_id, com) / (ip, tcp_port) 去重。

    - 同 node 同 com_name：保留 last_checked_at 最新一行，其余删除并写 audit；
    - 同 com_key（ip:tcp_port）跨 node：保留 online 且 last_checked 最新，其余 health_state=superseded。
    """
    import json
    from datetime import datetime

    from app.models import WireLogAudit, WireLogNode, WireLogPortBinding
    from app.utils import utcnow

    engine = session.get_bind()
    if "wl_port_binding" not in inspect(engine).get_table_names():
        return

    # 结构清理本身幂等：可重复跑
    rows = session.query(WireLogPortBinding).all()
    if not rows:
        return

    now = utcnow()
    deleted = 0
    superseded = 0

    # ---- (node_id, com_name) 去重 ----
    by_node_com: dict[tuple[str, str], list] = {}
    for b in rows:
        com = (b.com_name or "").strip()
        if not com:
            continue
        key = (b.node_id, com)
        by_node_com.setdefault(key, []).append(b)

    for (node_id, com), group in by_node_com.items():
        if len(group) < 2:
            continue

        def _seen(b):
            return b.last_checked_at or datetime.min

        group.sort(key=_seen, reverse=True)
        keeper = group[0]
        for dup in group[1:]:
            session.add(WireLogAudit(
                node_id=node_id,
                action="comres_dedupe_delete",
                actor="migrate",
                result="ok",
                detail=json.dumps({
                    "com": com, "kept_id": keeper.id, "deleted_id": dup.id,
                    "com_key": dup.com_key, "endpoint": dup.endpoint,
                }, ensure_ascii=False),
                created_at=now,
            ))
            session.delete(dup)
            deleted += 1

    session.flush()

    # ---- (ip, tcp_port) / com_key 跨 node：保留 online+最新，其余 superseded ----
    nodes = {n.node_id: n for n in session.query(WireLogNode).all()}
    remaining = (
        session.query(WireLogPortBinding)
        .filter(WireLogPortBinding.com_key.isnot(None))
        .filter(WireLogPortBinding.com_key != "")
        .all()
    )
    by_com_key: dict[str, list] = {}
    for b in remaining:
        hs = (b.health_state or "").lower()
        if hs == "superseded":
            continue
        by_com_key.setdefault(b.com_key, []).append(b)

    def _rank(b):
        node = nodes.get(b.node_id)
        online = 1 if node is not None and (node.status or "") == "online" else 0
        seen = b.last_checked_at or datetime.min
        return (online, seen)

    for com_key, group in by_com_key.items():
        if len(group) < 2:
            continue
        nodes_in = {b.node_id for b in group}
        if len(nodes_in) < 2:
            continue
        group.sort(key=_rank, reverse=True)
        keeper = group[0]
        for other in group[1:]:
            if other.node_id == keeper.node_id:
                continue
            was = other.health_state
            other.health_state = "superseded"
            session.add(WireLogAudit(
                node_id=other.node_id,
                action="comres_dedupe_supersede",
                actor="migrate",
                result="ok",
                detail=json.dumps({
                    "com_key": com_key, "com": other.com_name,
                    "kept_node": keeper.node_id, "kept_id": keeper.id,
                    "was_health_state": was,
                }, ensure_ascii=False),
                created_at=now,
            ))
            superseded += 1

    if deleted or superseded:
        session.commit()


def _backfill_wl_node_token_cipher(session: Session) -> None:
    """从 wl_client_apply 回填 wl_node.token_cipher（仅空值）。"""
    engine = session.get_bind()
    tables = set(inspect(engine).get_table_names())
    if "wl_node" not in tables or "wl_client_apply" not in tables:
        return
    cols = {c["name"] for c in inspect(engine).get_columns("wl_node")}
    if "token_cipher" not in cols:
        return
    session.execute(text(
        "UPDATE wl_node SET token_cipher = ("
        "  SELECT a.token_cipher FROM wl_client_apply a "
        "  WHERE a.issued_node_id = wl_node.node_id "
        "    AND a.token_cipher IS NOT NULL "
        "    AND a.status = 'approved' "
        "  ORDER BY a.approved_at DESC LIMIT 1"
        ") WHERE (token_cipher IS NULL OR token_cipher = '') "
        "AND EXISTS ("
        "  SELECT 1 FROM wl_client_apply a "
        "  WHERE a.issued_node_id = wl_node.node_id AND a.token_cipher IS NOT NULL"
        ")"
    ))
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
    # 空/NULL source 标 unknown，便于业务拒绝；不把既有 mock 改成 real，也不刷成 mock
    session.execute(
        text(
            "UPDATE device SET source = 'unknown' "
            "WHERE source IS NULL OR source = ''"
        )
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

def _rebuild_build_server_for_principal(session: Session) -> None:
    """v0.25 账号级改造：build_server 增加 principal_id（唯一），name 去掉全局唯一。

    空表（试用环境，无数据）直接 DROP 后按新模型重建；有数据则只做幂等 ALTER 加列，
    不破坏存量（name 上的旧唯一约束保留，不影响使用）。
    """
    from app import models  # noqa: F401 确保模型注册
    from app.db import Base

    engine = session.get_bind()
    insp = inspect(engine)
    if "build_server" not in insp.get_table_names():
        Base.metadata.create_all(engine, tables=[models.BuildServer.__table__])
        return
    cols = {c["name"] for c in insp.get_columns("build_server")}
    if "principal_id" in cols:
        return
    row_count = session.execute(text("SELECT COUNT(*) FROM build_server")).scalar()
    if row_count == 0:
        session.execute(text("DROP TABLE build_server"))
        session.commit()
        Base.metadata.create_all(engine, tables=[models.BuildServer.__table__])
    else:
        _ensure_columns(session, "build_server", {"principal_id": "INTEGER"})
        session.execute(
            text("CREATE UNIQUE INDEX IF NOT EXISTS uq_build_server_principal "
                 "ON build_server(principal_id)")
        )
        session.commit()


def migrate_serial_action_hex_steps(session: Session) -> dict:
    """幂等：把 SerialAction.steps 里旧 ``{"hex":...}`` 改写为 ``{"data":...,"encoding":"hex"}``。

    **不在** ``migrate()`` 自动执行——PT3a 仅供单测/运维手工调用。

    手工执行（生产库）示例::

        from app.db import SessionLocal
        from app.migrations import migrate_serial_action_hex_steps
        with SessionLocal() as s:
            print(migrate_serial_action_hex_steps(s))

    规则：
    - 仅有 ``hex``、无 ``data`` → 改为 data + encoding=hex，去掉 hex
    - ``data`` 与 ``hex`` 并存 → 以 data 为准，去掉 hex；缺 encoding 时补 hex
    - 已是 data/encoding → 不动
    """
    from app.models import SerialAction

    rows = session.query(SerialAction).all()
    scanned = 0
    rewritten = 0
    unchanged = 0
    for act in rows:
        scanned += 1
        steps = act.steps
        if not isinstance(steps, list) or not steps:
            unchanged += 1
            continue
        new_steps: list = []
        changed = False
        for raw in steps:
            if not isinstance(raw, dict):
                new_steps.append(raw)
                continue
            step = dict(raw)
            has_data = step.get("data") is not None and str(step.get("data")) != ""
            has_hex = step.get("hex") is not None and str(step.get("hex")) != ""
            if has_hex and not has_data:
                step["data"] = step.pop("hex")
                if not step.get("encoding"):
                    step["encoding"] = "hex"
                changed = True
            elif has_hex and has_data:
                step.pop("hex", None)
                if not step.get("encoding"):
                    step["encoding"] = "hex"
                changed = True
            new_steps.append(step)
        if changed:
            act.steps = new_steps
            rewritten += 1
        else:
            unchanged += 1
    if rewritten:
        session.commit()
    return {
        "ok": True,
        "scanned": scanned,
        "rewritten": rewritten,
        "unchanged": unchanged,
    }

