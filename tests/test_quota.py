"""配额上限：holder 并发上限默认 2。"""
from __future__ import annotations

from app.mcp.server import res_apply

from tests.conftest import register_device
from tests.mock_device import MockDevice


def test_quota_exceeded(db):
    devices = []
    ids = []
    for i in range(3):
        dev = MockDevice(model="QM102V").start()
        reg = register_device(db, "user01@corp.com", dev, f"302-R5-{i:02d}", "owner@corp.com")
        devices.append(dev)
        ids.append(reg["device_id"])

    try:
        r1 = db.call("user02@corp.com", res_apply, target=ids[0])
        assert r1["state"] == "GRANTED"
        r2 = db.call("user02@corp.com", res_apply, target=ids[1])
        assert r2["state"] == "GRANTED"
        r3 = db.call("user02@corp.com", res_apply, target=ids[2])
        assert r3.get("error") == "QUOTA_EXCEEDED", r3
    finally:
        for d in devices:
            d.stop()


def test_admin_can_lease_with_zero_quota(db):
    """管理员 quota=0 只是不占普通用户配额，租约入口应豁免配额门禁。"""
    from app.mcp.server import res_release, res_whoami

    admin = db.call("admin@corp.com", res_whoami)
    assert admin["is_admin"] is True
    assert admin["quota"] == 0

    devices = []
    lease_ids = []
    try:
        # 连续租 2 台：quota=0 也不应触发 QUOTA_EXCEEDED（普通用户默认上限 2）
        for i in range(2):
            dev = MockDevice(model="QM102V").start()
            devices.append(dev)
            reg = register_device(
                db, "user01@corp.com", dev, f"302-R6-{i:02d}", "owner@corp.com"
            )
            r = db.call("admin@corp.com", res_apply, target=reg["device_id"], purpose="admin 验证")
            assert r["state"] == "GRANTED", r
            lease_ids.append(r["lease"]["lease_id"])

        # 管理员的租约不占用其配额语义：quota 仍为 0，但仍能继续持有
        who = db.call("admin@corp.com", res_whoami)
        assert who["quota"] == 0
        assert who["active_leases"] == 2
    finally:
        for lease_id in lease_ids:
            db.call("admin@corp.com", res_release, lease_id=lease_id, reason="test cleanup")
        for d in devices:
            d.stop()
