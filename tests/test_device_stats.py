"""设备中心统计口径：总量 / 在线 / 离线 / 待检 / 已分配 / 空闲 与 mock 数据一致。

对齐 /api/dashboard 的 by_status 与前端 deviceTotals：
- total = 非 retired
- online = available + occupied
- allocated(已分配) = occupied
- idle(空闲) = available（状态计数，含 suspect）
"""
from __future__ import annotations

from datetime import datetime

from app.api.router import dashboard
from app.constants import DeviceStatus, HealthLevel
from app.models import Device, ProductType


def _ensure_product(session, code="QM102V"):
    if session.get(ProductType, code) is not None:
        return
    session.add(
        ProductType(
            type_code=code,
            display_name=code,
            has_screen=True,
            has_led=False,
            serial_count=2,
            adb_port=5555,
            enabled=True,
            created_at=datetime(2026, 1, 1),
        )
    )
    session.commit()


def _add(session, *, device_id, status, health=HealthLevel.L3, model="QM102V"):
    _ensure_product(session, model)
    d = Device(
        device_id=device_id,
        display_name=device_id,
        model_class=model,
        position_code=f"T-{device_id[-4:]}",
        owner="owner@corp.com",
        status=status,
        health_level=health,
        ip=f"10.20.0.{abs(hash(device_id)) % 200 + 1}",
        adb_port=5555,
        source="real",
        consecutive_failures=0,
        registered_by="admin@corp.com",
        register_channel="web_manual",
        created_at=datetime(2026, 1, 1),
    )
    session.add(d)
    session.commit()
    return d


def _dash(db) -> dict:
    s = db.session()
    try:
        return dashboard(session=s)
    finally:
        s.close()


def test_dashboard_stats_match_mock_devices(db):
    s = db.session()
    try:
        # 2 available + 1 occupied + 2 offline + 3 pending + 1 retired(不计总量)
        _add(s, device_id="st_av1", status=DeviceStatus.AVAILABLE)
        _add(s, device_id="st_av2", status=DeviceStatus.AVAILABLE)
        _add(s, device_id="st_oc1", status=DeviceStatus.OCCUPIED)
        _add(s, device_id="st_of1", status=DeviceStatus.OFFLINE, health=HealthLevel.L0)
        _add(s, device_id="st_of2", status=DeviceStatus.OFFLINE, health=HealthLevel.L0)
        _add(s, device_id="st_pe1", status=DeviceStatus.PENDING, health=HealthLevel.L0)
        _add(s, device_id="st_pe2", status=DeviceStatus.PENDING, health=HealthLevel.L0)
        _add(s, device_id="st_pe3", status=DeviceStatus.PENDING, health=HealthLevel.L0)
        _add(s, device_id="st_re1", status=DeviceStatus.RETIRED, health=HealthLevel.L0)
    finally:
        s.close()

    d = _dash(db)
    by = d["by_status"]
    available = by.get(DeviceStatus.AVAILABLE, by.get("available", 0))
    occupied = by.get(DeviceStatus.OCCUPIED, by.get("occupied", 0))
    offline = by.get(DeviceStatus.OFFLINE, by.get("offline", 0))
    pending = by.get(DeviceStatus.PENDING, by.get("pending", 0))

    assert d["total_devices"] == 8
    assert available == 2
    assert occupied == 1
    assert offline == 2
    assert pending == 3
    assert d["retired_count"] == 1

    # 前端口径
    online = available + occupied
    allocated = occupied
    idle = available
    assert online == 3
    assert allocated == 1
    assert idle == 2
