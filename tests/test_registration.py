"""设备注册：probe -> register 两段式；探不到的设备登记为 pending 不进可用池。"""
from __future__ import annotations

from app.constants import DeviceStatus, HealthLevel
from app.mcp.server import res_apply, res_list_devices, res_list_products
from app.models import Device

from tests.conftest import register_device
from tests.mock_device import MockDevice, start_devices

MODELS = ["QM102V", "QM10XD", "QM108H", "QM102V-Lite", "BES2800BP"]


def test_register_20_devices(db):
    devices = start_devices(20, models=MODELS, base_asset="ASSET")
    try:
        owner = "owner@corp.com"
        ids = set()
        for i, dev in enumerate(devices):
            pos = f"302-R{(i % 3) + 1}-{i:02d}"
            reg = register_device(db, "user01@corp.com", dev, pos, owner)
            assert reg["status"] == DeviceStatus.AVAILABLE, reg
            assert reg["matched_as"] == "new"
            ids.add(reg["device_id"])
        assert len(ids) == 20

        products = db.call("user01@corp.com", res_list_products)
        total = sum(p["available"] for p in products)
        assert total == 20
    finally:
        for d in devices:
            d.stop()


def test_probe_unreachable_registers_pending(db):
    dev = MockDevice(model="QM102V").start()
    try:
        # L1：端口响应但 adbd 没起 -> 探不到，登记为 pending，不进池
        dev.set_level("L1")
        reg = register_device(db, "user01@corp.com", dev, "302-R2-01", "owner@corp.com")
        assert reg["status"] == DeviceStatus.PENDING
        device_id = reg["device_id"]

        # pending 设备不出现在 available 列表
        avail = db.call("user01@corp.com", res_list_devices, status=DeviceStatus.AVAILABLE)
        assert all(d["device_id"] != device_id for d in avail)

        # 按机型申请拿不到它（该机型没有 L3 设备）
        r = db.call("user01@corp.com", res_apply, target="QM102V")
        assert r["state"] == "QUEUED", r
    finally:
        dev.stop()


def test_unknown_model_does_not_block(db):
    # 机型不在字典 -> unknown_model，但能 adb 就能入池
    dev = MockDevice(model="UNKNOWN_MODEL_XYZ").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "302-R3-01", "owner@corp.com")
        assert reg["status"] == DeviceStatus.AVAILABLE
        # 按机型筛不到它（model_class 为空）
        assert reg["matched_as"] == "new"
    finally:
        dev.stop()


def test_position_duplicate_rejected(db):
    dev1 = MockDevice(model="QM102V").start()
    dev2 = MockDevice(model="QM102V").start()
    try:
        register_device(db, "user01@corp.com", dev1, "302-R4-01", "owner@corp.com")
        r = register_device(db, "user01@corp.com", dev2, "302-R4-01", "owner@corp.com")
        assert r.get("error") == "POSITION_DUP", r
    finally:
        dev1.stop()
        dev2.stop()


def test_flash_confirm_branch(db):
    """无屏无灯机型（BES2800BP）点亮核对不可用。"""
    from app.mcp.server import res_device_flash, res_device_probe

    dev = MockDevice(model="BES2800BP").start()
    try:
        probe = db.call("user01@corp.com", res_device_probe, ip=dev.ip, port=dev.port)
        flash = db.call("user01@corp.com", res_device_flash, form_id=probe["form_id"])
        assert flash["how"] == "none", flash  # 无屏无灯 -> 跳过
    finally:
        dev.stop()


def test_unregister_with_active_lease_pending_removal(db):
    from app.mcp.server import res_apply, res_device_unregister, res_release

    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "302-R4-02", "owner@corp.com")
        device_id = reg["device_id"]

        # user02 占用
        r = db.call("user02@corp.com", res_apply, target=device_id)
        assert r["state"] == "GRANTED"
        lease_id = r["lease"]["lease_id"]

        # 注册人 user01 注销 -> 有活动租约，拒绝 + 标 pending_removal
        u = db.call("user01@corp.com", res_device_unregister, device_id=device_id)
        assert u.get("error") == "HAS_ACTIVE_LEASE", u

        s = db.session()
        d = s.get(Device, device_id)
        assert d.status == DeviceStatus.PENDING_REMOVAL
        s.close()

        # 租约结束后自动摘除
        db.call("user02@corp.com", res_release, lease_id=lease_id)
        s = db.session()
        d = s.get(Device, device_id)
        assert d.status == DeviceStatus.RETIRED
        s.close()
    finally:
        dev.stop()
