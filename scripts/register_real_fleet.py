#!/usr/bin/env python3
"""注册真实 Quamm XOS 设备机队到主库。

用 RealXosProber（adb connect + shell 读文件，无 getprop）逐台探测 14 台局域网
设备并直接入库（走领域模型 Device，不经 MCP 两段式表单）：

- 能连上且读到 product_name + 固定 mac 的：L3 / available
- 连不上或探测不完整的：**仍"强行添加"为 pending**（用户明确要求：探测不完整
  也要入库用于后续联调 / 测试），不因字典不匹配而拒绝
- model_class：匹配不到现有 5 类字典时，用 project_name 本身作为类型（并补
  一条 product_type 记录以满足外键；见 _ensure_product_type）
- 幂等：按 mac 或 (ip, source=real) 命中已有记录则更新，不新建

只读 adb 文件，绝不 push / remount / 刷机。每条 adb 命令在 RealXosProber 内
已用 GNU timeout + subprocess 超时双重包裹。

用法：
  .venv/bin/python scripts/register_real_fleet.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.constants import DeviceStatus, HealthLevel  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.domain import audit  # noqa: E402
from app.domain.prober_real import RealXosProber  # noqa: E402
from app.migrations import migrate  # noqa: E402
from app.models import Device, ProductType  # noqa: E402
from app.utils import new_device_id, utcnow  # noqa: E402

# (ip, adbscan 报的 product_name 提示，仅供对照；真实值以 RealXosProber 结果为准)
FLEET: list[tuple[str, str | None]] = [
    ("172.16.10.192", "moto_ld"),
    ("172.16.10.20", None),
    ("172.16.10.246", "qm10xv_full"),
    ("172.16.20.206", "qm10xh_full"),
    ("172.16.20.212", "esl2"),
    ("172.16.20.71", "esl2"),
    ("172.16.60.104", None),
    ("172.16.60.16", None),
    ("172.16.60.245", "framex"),
    ("172.16.60.49", "framex"),
    ("172.16.60.57", None),
    ("172.16.60.85", None),
    ("172.16.60.87", "qm10xh_full"),
    ("172.16.60.92", None),
]

PORT = 5555
OWNER = "auto-import"
CHANNEL = "auto-import"


def _ensure_product_type(session, type_code: str | None) -> str | None:
    """model_class 用 project_name 本身作为类型；不在 5 类字典时补一条字典记录。

    device.model_class 有指向 product_type 的外键（SQLite PRAGMA foreign_keys=ON），
    直接塞字典里不存在的值会触发 FOREIGN KEY constraint failed —— 这正是
    "因字典不匹配而拒绝注册"。补一条记录既不拒绝，又让 model_class=工程配置名生效。
    """
    if not type_code:
        return None
    if session.get(ProductType, type_code) is None:
        session.add(
            ProductType(
                type_code=type_code,
                display_name=type_code,
                has_screen=False,
                has_led=False,
                serial_count=0,
                adb_port=PORT,
                enabled=True,
                model_pattern=None,
                created_at=utcnow(),
            )
        )
        session.flush()
    return type_code


def _match_existing(session, ip: str, mac: str | None) -> Device | None:
    """强锚 mac 优先；否则按 (ip, source=real) 命中已登记的同一台设备。"""
    if mac:
        row = session.query(Device).filter(Device.mac == mac).first()
        if row is not None:
            return row
    return (
        session.query(Device)
        .filter(Device.ip == ip, Device.source == "real")
        .first()
    )


def _free_position(session, idx: int, own_id: str | None) -> str:
    """REAL-AUTO-01 起递增；若被别的设备占用则顺延，避免唯一索引冲突。"""
    n = idx
    while True:
        pos = f"REAL-AUTO-{n:02d}"
        row = session.query(Device).filter(Device.position_code == pos).first()
        if row is None or row.device_id == own_id:
            return pos
        n += 1


def _pad(text, width: int) -> str:
    s = str(text)
    # 中文按 2 列宽估算，表格对齐用
    w = sum(2 if ord(ch) > 127 else 1 for ch in s)
    return s + " " * max(0, width - w)


def main() -> int:
    init_db()
    session = SessionLocal()
    try:
        # 老库不丢数据：幂等补 device 新列，并把既有 mock 设备标 source=mock
        migrate(session)

        prober = RealXosProber()
        rows: list[dict] = []

        for idx, (ip, hint) in enumerate(FLEET, start=1):
            try:
                result = prober.probe(ip, PORT)
                extra = result.extra
                level = result.level
            except Exception as exc:  # noqa: BLE001
                print(f"[warn] probe {ip} raised {type(exc).__name__}: {exc}; 记为 pending")
                extra = {}
                level = HealthLevel.L0

            product_name = extra.get("product_name")
            fixed_mac = extra.get("fixed_mac")
            storage_type = extra.get("storage_type", "unknown")
            board_dt = extra.get("board_dt")
            has_wifi = bool(extra.get("has_wifi", False))
            is_l3 = level == HealthLevel.L3

            existing = _match_existing(session, ip, fixed_mac)
            model_class = _ensure_product_type(session, product_name)
            position = _free_position(session, idx, existing.device_id if existing else None)
            now = utcnow()

            if existing is not None:
                d = existing
                d.ip = ip
                d.adb_port = PORT
                d.mac = fixed_mac or d.mac
                d.project_name = product_name or d.project_name
                d.board_dt = board_dt or d.board_dt
                d.storage_type = storage_type
                d.has_wifi = has_wifi
                d.source = "real"
                d.owner = OWNER
                d.model_class = model_class or d.model_class
                d.display_name = f"{product_name or 'pending'}@{ip}"
                matched_as = "existing"
            else:
                d = Device(
                    device_id=new_device_id(),
                    display_name=f"{product_name or 'pending'}@{ip}",
                    model_class=model_class,
                    mac=fixed_mac,
                    sn=None,
                    asset_tag=None,
                    position_code=position,
                    owner=OWNER,
                    status=DeviceStatus.PENDING,
                    health_level=level,
                    registered_by=OWNER,
                    register_channel=CHANNEL,
                    ip=ip,
                    adb_port=PORT,
                    android_version=None,
                    build_fingerprint=None,
                    project_name=product_name,
                    board_dt=board_dt,
                    storage_type=storage_type,
                    has_wifi=has_wifi,
                    source="real",
                    consecutive_failures=0,
                    last_seen_at=now if is_l3 else None,
                    last_probe_at=now,
                    created_at=now,
                )
                session.add(d)
                session.flush()
                matched_as = "new"

            # L3 才入池，其余一律 pending（探测不完整也入库）
            if is_l3:
                d.status = DeviceStatus.AVAILABLE
                d.health_level = HealthLevel.L3
                d.consecutive_failures = 0
                d.last_seen_at = now
            else:
                d.status = DeviceStatus.PENDING
                d.health_level = level
            d.last_probe_at = now

            audit.record(
                session, actor=OWNER, action="register_real_fleet",
                device_id=d.device_id,
                detail={
                    "ip": ip, "project_name": product_name, "level": d.health_level,
                    "matched_as": matched_as, "position_code": d.position_code,
                },
            )
            session.commit()

            rows.append(
                {
                    "ip": ip,
                    "product_name": product_name or "-",
                    "mac": fixed_mac or "-",
                    "storage": storage_type,
                    "level": d.health_level,
                    "status": d.status,
                    "position": d.position_code,
                    "device_id": d.device_id,
                    "hint": hint or "-",
                    "matched_as": matched_as,
                }
            )
            print(
                f"[{idx:02d}] {ip:<16} product={product_name or '-':<12} "
                f"mac={fixed_mac or '-':<18} storage={storage_type:<8} "
                f"level={d.health_level} status={d.status:<9} {matched_as}"
            )

        # ---- 结果表 ----
        headers = ["IP", "product_name", "固定mac", "storage", "level", "status", "position", "adbscan提示"]
        keys = ["ip", "product_name", "mac", "storage", "level", "status", "position", "hint"]
        widths = [
            max(len(h), *(len(str(r[k])) for r in rows)) for h, k in zip(headers, keys)
        ]
        print()
        print("=== 14 台真实设备探测 / 注册结果 ===")
        print("  ".join(_pad(h, w) for h, w in zip(headers, widths)))
        print("  ".join("-" * w for w in widths))
        for r in rows:
            print("  ".join(_pad(r[k], w) for k, w in zip(keys, widths)))

        # ---- 主库复核（以 DB 为准）----
        real = session.query(Device).filter(Device.source == "real").all()
        available = sum(1 for d in real if d.status == DeviceStatus.AVAILABLE)
        pending = sum(1 for d in real if d.status == DeviceStatus.PENDING)
        mock = session.query(Device).filter(Device.source == "mock").count()
        print()
        print(f"DB 复核：source=real {len(real)} 台（available {available} / pending {pending}），"
              f"source=mock {mock} 台（原有 22 台不受影响）")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
