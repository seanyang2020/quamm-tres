"""巡检闭环：可用 → 单轮 suspect（掉出可用池）→ 连续 2 轮 offline + notice → 恢复回池。

同时锁定"注册与巡检共用同一 Prober"与"可用池口径"：
- suspect 默认不可申请，dashboard 的 available 按真实可申请数回落
- settings.allow_apply_when_suspect=True 时才允许降级设备被申请
"""
from __future__ import annotations

from app.api.router import dashboard
from app.config import settings
from app.constants import DeviceStatus, HealthLevel
from app.domain import patrol
from app.domain.probe import AdbProber, MockProber, build_prober
from app.mcp.server import res_apply
from app.models import Device, Notice

from tests.conftest import register_device
from tests.mock_device import MockDevice


def _register_many(db, count: int) -> list[tuple[MockDevice, str]]:
    out = []
    for i in range(count):
        dev = MockDevice(model="QM102V").start()
        reg = register_device(
            db, "user01@corp.com", dev, f"302-R7-{i:02d}", "owner@corp.com"
        )
        assert reg["status"] == DeviceStatus.AVAILABLE, reg
        out.append((dev, reg["device_id"]))
    return out


def _dash(db) -> dict:
    s = db.session()
    try:
        return dashboard(session=s)
    finally:
        s.close()


def test_patrol_cycle_offline_and_recover(db):
    pairs = _register_many(db, 5)
    devices = [p[0] for p in pairs]
    ids = [p[1] for p in pairs]
    try:
        # 阶段 1：全部在线 -> l3=5，可用=5
        run1 = patrol.run_patrol_round(db.session())
        assert (run1.scanned_count, run1.l3_count, run1.suspect_count, run1.offline_count) == (5, 5, 0, 0)
        d1 = _dash(db)
        assert d1["available"] == 5 and d1["suspect"] == 0

        # 阶段 2：关掉 2 台 -> 单轮 suspect，可用池降到 3
        devices[0].stop()
        devices[1].stop()
        run2 = patrol.run_patrol_round(db.session())
        assert (run2.l3_count, run2.suspect_count, run2.offline_count) == (3, 2, 0), (
            run2.l3_count, run2.suspect_count, run2.offline_count
        )
        d2 = _dash(db)
        assert d2["available"] == 3, d2
        assert d2["suspect"] == 2, d2
        assert d2["by_status"]["available"] == 3

        s = db.session()
        d = s.get(Device, ids[0])
        assert d.status == DeviceStatus.AVAILABLE  # 单轮仍 available
        assert d.health_level == HealthLevel.L0    # 但健康降级，掉出可用池
        s.close()

        # suspect 默认不可申请 -> 排队
        r = db.call("user01@corp.com", res_apply, target=ids[0])
        assert r["state"] == "QUEUED", r

        # 阶段 3：仍关着 -> 连续 2 轮 offline + notice
        run3 = patrol.run_patrol_round(db.session())
        assert (run3.l3_count, run3.offline_count) == (3, 2), (run3.l3_count, run3.offline_count)
        s = db.session()
        for device_id in ids[:2]:
            d = s.get(Device, device_id)
            assert d.status == DeviceStatus.OFFLINE
            assert s.query(Notice).filter(
                Notice.device_id == device_id, Notice.event_type == "device_offline"
            ).count() == 1
        s.close()
        assert _dash(db)["available"] == 3

        # 阶段 4：重新开机 -> L3 恢复，回池 available=5
        devices[0].start()
        devices[1].start()
        run4 = patrol.run_patrol_round(db.session())
        assert (run4.l3_count, run4.suspect_count, run4.offline_count) == (5, 0, 0)
        d4 = _dash(db)
        assert d4["available"] == 5, d4
        assert d4["suspect"] == 0
        s = db.session()
        for device_id in ids:
            d = s.get(Device, device_id)
            assert d.status == DeviceStatus.AVAILABLE
            assert d.health_level == HealthLevel.L3
            assert d.consecutive_failures == 0
        s.close()
    finally:
        for dev in devices:
            dev.stop()


def test_suspect_not_claimable_unless_configured(db, monkeypatch):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "302-R8-01", "owner@corp.com")
        device_id = reg["device_id"]

        # L1 = 端口可响应但 adbd 不可用 -> suspect
        dev.set_level("L1")
        patrol.run_patrol_round(db.session())
        s = db.session()
        d = s.get(Device, device_id)
        assert d.status == DeviceStatus.AVAILABLE
        assert d.health_level == HealthLevel.L1
        s.close()
        assert _dash(db)["available"] == 0

        # 默认开关：不可申请 -> 排队
        r = db.call("user01@corp.com", res_apply, target=device_id)
        assert r["state"] == "QUEUED", r

        # 打开降级可用开关：L1 可以被申请
        monkeypatch.setattr(settings, "allow_apply_when_suspect", True)
        assert _dash(db)["available"] == 1
        r2 = db.call("user01@corp.com", res_apply, target=device_id)
        assert r2["state"] == "GRANTED", r2
    finally:
        monkeypatch.setattr(settings, "allow_apply_when_suspect", False)
        dev.stop()


def test_prober_backend_selection_and_no_stub():
    assert isinstance(build_prober("mock"), MockProber)
    assert isinstance(build_prober("real"), AdbProber)
    assert isinstance(build_prober(None), MockProber)
    assert isinstance(build_prober("unknown"), MockProber)
