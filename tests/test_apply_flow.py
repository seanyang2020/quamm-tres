"""端到端：申请 -> 续约 -> 释放（真实接口层）。"""
from __future__ import annotations

from app.constants import DeviceStatus, LeaseState
from app.mcp.server import res_apply, res_release, res_renew

from tests.conftest import register_device
from tests.mock_device import MockDevice


def test_apply_renew_release(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "302-R1-01", "owner@corp.com")
        assert reg["status"] == DeviceStatus.AVAILABLE
        device_id = reg["device_id"]

        # 申请 -> GRANTED + adb_address
        r = db.call("user01@corp.com", res_apply, target=device_id, purpose="验证", expected_minutes=40)
        assert r["state"] == "GRANTED", r
        lease_id = r["lease"]["lease_id"]
        assert r["lease"]["adb_address"] == dev.adb_address

        # 续约 -> 到期时间后延
        before = r["lease"]["expires_at"]
        rr = db.call("user01@corp.com", res_renew, lease_id=lease_id, extra_minutes=30)
        assert rr["state"] == LeaseState.GRANTED
        assert rr["expires_at"] > before

        # 释放 -> 幂等
        rel = db.call("user01@corp.com", res_release, lease_id=lease_id, reason="done")
        assert rel["ok"] is True
        rel2 = db.call("user01@corp.com", res_release, lease_id=lease_id)
        assert rel2["ok"] is True
        assert rel2.get("idempotent") is True
    finally:
        dev.stop()


def test_list_products_and_whoami(db):
    from app.mcp.server import res_list_products, res_whoami

    products = db.call("user01@corp.com", res_list_products)
    assert len(products) == 5
    codes = {p["type_code"] for p in products}
    assert {"QM102V", "QM10XD", "QM108H", "QM102V-Lite", "BES2800BP"} <= codes

    who = db.call("user01@corp.com", res_whoami)
    assert who["email"] == "user01@corp.com"
    assert who["quota"] == 2
    assert who["is_admin"] is False  # 普通账号不是管理员


def test_whoami_admin_flag(db):
    from app.mcp.server import res_whoami

    admin = db.call("admin@corp.com", res_whoami)
    assert admin["email"] == "admin@corp.com"
    assert admin["is_admin"] is True  # 仅 admin@corp.com 是管理员
