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


def test_product_metadata_completeness_and_stats(db):
    """v0.12：6 字段 + 完整度标签 + total/available/leased/queued 统计。"""
    from app.mcp.server import res_list_products
    from app.models import ProductType

    products = {p["type_code"]: p for p in db.call("user01@corp.com", res_list_products)}
    assert len(products) == 5
    q = products["QM102V"]
    assert q["complete"] is False and q["complete_label"] == "信息不全"
    for f in ("leased", "queued", "defconfig", "sdk_repo_cmd", "build_cmd",
              "image_path", "jenkins_url", "notes"):
        assert f in q

    dev = MockDevice(model="QM102V").start()
    try:
        register_device(db, "user01@corp.com", dev, "302-R1-01", "owner@corp.com")
        q = {p["type_code"]: p for p in db.call("user01@corp.com", res_list_products)}["QM102V"]
        assert q["total"] == 1 and q["available"] == 1 and q["leased"] == 0 and q["queued"] == 0

        r = db.call("user01@corp.com", res_apply, product_type="QM102V")
        assert r["state"] == "GRANTED"
        q = {p["type_code"]: p for p in db.call("user01@corp.com", res_list_products)}["QM102V"]
        assert q["leased"] == 1

        db.call("user02@corp.com", res_apply, product_type="QM102V")
        q = {p["type_code"]: p for p in db.call("user01@corp.com", res_list_products)}["QM102V"]
        assert q["queued"] == 1
        db.call("user01@corp.com", res_release, lease_id=r["lease"]["lease_id"])
    finally:
        dev.stop()

    # 填齐四列 -> 完整；缺任一 -> 回到信息不全
    s = db.session()
    pt = s.get(ProductType, "QM102V")
    pt.defconfig = "project_qm102v_defconfig"
    pt.sdk_repo_cmd = "repo init -u http://x/manifest; repo sync -j8"
    pt.build_cmd = "make distclean && make project_qm102v_defconfig && make xos -j8"
    pt.image_path = "out/qm102v_linux/qmimages/"
    s.commit()
    s.close()
    q = {p["type_code"]: p for p in db.call("user01@corp.com", res_list_products)}["QM102V"]
    assert q["complete"] is True and q["complete_label"] == "完整"

    s = db.session()
    s.get(ProductType, "QM102V").image_path = None
    s.commit()
    s.close()
    q = {p["type_code"]: p for p in db.call("user01@corp.com", res_list_products)}["QM102V"]
    assert q["complete"] is False
