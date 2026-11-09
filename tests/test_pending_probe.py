"""v0.28：pending 设备快速探测 + 申请时触发探测。

- 运行时新增 pending_patrol_interval_minutes / probe_backend 校验
- 申请时若没有可用设备、只剩 pending，则立即探测一次，探通直接分配
- 设备自身 source 分流（mock 设备仍走 mock）
"""
from __future__ import annotations

from datetime import datetime

from app.constants import DeviceStatus, HealthLevel
from app.domain.probe import ProbeResult
from app.models import Device, ProductType
from app.runtime_config import validate_runtime_value
def _ensure_product(session, code):
    """测试库的机型字典为空，按需补一行（真实库由 seed/设置页维护）。"""
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


def _add_pending(db, *, device_id="dev_pending001", ip="10.10.0.9", model="QM102V",
                 source="real", locked=False):
    s = db.session()
    try:
        _ensure_product(s, model)
        d = Device(
            device_id=device_id,
            display_name=f"pending-{device_id}",
            model_class=model,
            position_code=f"302-R9-{device_id[-3:]}",
            owner="owner@corp.com",
            status=DeviceStatus.PENDING,
            health_level=HealthLevel.L0,
            ip=ip,
            adb_port=5555,
            source=source,
            consecutive_failures=3,
            registered_by="admin@corp.com",
            register_channel="web_manual",
            created_at=datetime(2026, 1, 1),
        )
        d.locked = locked
        s.add(d)
        s.commit()
    finally:
        s.close()
    return device_id


def _l3_probe(host, port=5555, timeout=5, source=None):
    result = ProbeResult(level=HealthLevel.L3, mac="aa:bb:cc:dd:ee:ff", model="QM102V")
    result.extra = {"firmware_version": "1.0.0"}
    return result


def test_runtime_keys_validation():
    ok, err = validate_runtime_value("pending_patrol_interval_minutes", 10)
    assert err is None and ok == 10
    _, err = validate_runtime_value("pending_patrol_interval_minutes", 0)
    assert err and "1" in err

    ok, err = validate_runtime_value("probe_backend", "real")
    assert err is None and ok == "real"
    _, err = validate_runtime_value("probe_backend", "fake")
    assert err and "mock" in err


def test_apply_probes_pending_device_and_grants(db, monkeypatch):
    """没有可用设备、只有 pending 设备时，apply 触发一次探测并直接分配。"""
    from app.mcp.server import res_apply

    _add_pending(db, device_id="dev_pending011")
    monkeypatch.setattr("app.domain.leasing.probe_device", _l3_probe)

    r = db.call(
        "user01@corp.com", res_apply, product_type="QM102V",
        purpose="pending 触发探测", agent_info="pytest-agent/1.0",
    )
    assert r["state"] == "GRANTED", r
    assert r["lease"]["device_id"] == "dev_pending011"

    s = db.session()
    try:
        d = s.get(Device, "dev_pending011")
        assert d.status == DeviceStatus.OCCUPIED
        assert d.health_level == HealthLevel.L3
        assert d.consecutive_failures == 0
    finally:
        s.close()


def test_apply_falls_back_to_queue_when_probe_fails(db, monkeypatch):
    """探测失败的 pending 设备不参与分配，仍走排队。"""
    from app.mcp.server import res_apply

    _add_pending(db, device_id="dev_pending012")
    monkeypatch.setattr(
        "app.domain.leasing.probe_device",
        lambda host, port=5555, timeout=5, source=None: ProbeResult(level=HealthLevel.L0),
    )

    r = db.call(
        "user01@corp.com", res_apply, product_type="QM102V",
        purpose="探测失败排队", agent_info="pytest-agent/1.0",
    )
    assert r["state"] == "QUEUED", r

    s = db.session()
    try:
        d = s.get(Device, "dev_pending012")
        assert d.status == DeviceStatus.PENDING
        assert d.health_level == HealthLevel.L0
        assert d.consecutive_failures == 4  # 失败计数 +1
        assert d.source == "real"
    finally:
        s.close()


def test_pending_probe_skips_locked_and_other_models(db, monkeypatch):
    """只探目标机型、且跳过手动锁定设备。"""
    from app.domain.leasing import _probe_pending_for_apply

    _add_pending(db, device_id="dev_pending021", model="QM102V")
    _add_pending(db, device_id="dev_pending022", model="QM102V", locked=True)
    _add_pending(db, device_id="dev_pending023", model="QM10XD")

    calls = []

    def fake_probe(host, port=5555, timeout=5, source=None):
        calls.append(host)
        return ProbeResult(level=HealthLevel.L0)

    monkeypatch.setattr("app.domain.leasing.probe_device", fake_probe)
    s = db.session()
    try:
        promoted = _probe_pending_for_apply(
            s, "product", "QM102V", actor="admin@corp.com"
        )
        assert promoted == 0
        assert calls == ["10.10.0.9"]
    finally:
        s.close()

def _add_device(db, *, device_id, status, model="QM102V", ip="10.10.0.7", health="L3",
                failures=0, source="real"):
    s = db.session()
    try:
        _ensure_product(s, model)
        d = Device(
            device_id=device_id,
            display_name=f"dev-{device_id}",
            model_class=model,
            position_code=f"302-R8-{device_id[-3:]}",
            owner="owner@corp.com",
            status=status,
            health_level=health,
            ip=ip,
            adb_port=5555,
            source=source,
            consecutive_failures=failures,
            registered_by="admin@corp.com",
            register_channel="web_manual",
            created_at=datetime(2026, 1, 1),
        )
        s.add(d)
        s.commit()
    finally:
        s.close()


def _l0_probe(host, port=5555, timeout=5, source=None):
    return ProbeResult(level=HealthLevel.L0)


def test_manual_probe_demotes_available_to_offline(db, monkeypatch):
    """人工探测失败：available 立即降级为 offline，不等巡检阈值。"""
    from app.domain.catalog import device_probe_registered

    _add_device(db, device_id="dev_manual01", status=DeviceStatus.AVAILABLE)
    monkeypatch.setattr("app.domain.catalog.probe_device", _l0_probe)

    s = db.session()
    try:
        r = device_probe_registered(
            s, "dev_manual01", actor_email="admin@corp.com", is_admin=True
        )
        assert r["demoted"] is True, r
        assert r["status"] == DeviceStatus.OFFLINE
        assert r["level"] == HealthLevel.L0
        d = s.get(Device, "dev_manual01")
        assert d.status == DeviceStatus.OFFLINE
        assert d.health_level == HealthLevel.L0
        assert d.consecutive_failures == 1
    finally:
        s.close()


def test_manual_probe_keeps_available_when_probe_ok(db, monkeypatch):
    """人工探测成功：available 保持 available，健康度刷新为 L3、失败计数清零。"""
    from app.domain.catalog import device_probe_registered

    _add_device(db, device_id="dev_manual02", status=DeviceStatus.AVAILABLE,
                health="L1", failures=2)
    monkeypatch.setattr("app.domain.catalog.probe_device", _l3_probe)

    s = db.session()
    try:
        r = device_probe_registered(
            s, "dev_manual02", actor_email="admin@corp.com", is_admin=True
        )
        assert r["demoted"] is False and r["promoted"] is False, r
        d = s.get(Device, "dev_manual02")
        assert d.status == DeviceStatus.AVAILABLE
        assert d.health_level == HealthLevel.L3
        assert d.consecutive_failures == 0
    finally:
        s.close()
