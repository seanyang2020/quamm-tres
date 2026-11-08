"""v0.16：运行时 settings PUT、机型删除引用闸、无邮箱凭据生成。"""
from __future__ import annotations

from starlette.testclient import TestClient

from app.constants import LeaseState, QueueState
from app.main import app
from app.models import Credential, Device, Lease, Principal, ProductType, QueueEntry
from app.runtime_config import get_runtime_value
from app.utils import new_lease_id, utcnow


def _admin_client(db) -> tuple[TestClient, str]:
    """用 admin token 登录拿到带 cookie 的 TestClient。"""
    token = db.token("admin@corp.com")
    client = TestClient(app)
    r = client.post("/api/auth/login", json={"token": token})
    assert r.status_code == 200, r.text
    return client, token


def test_put_settings_whitelist_and_bounds(db):
    client, _ = _admin_client(db)

    # 越界
    r = client.put("/api/settings", json={"patrol_interval_minutes": 0})
    assert r.status_code == 400, r.text
    body = r.json()["detail"]
    assert "fields" in body and "patrol_interval_minutes" in body["fields"]

    r = client.put("/api/settings", json={"holder_concurrency_limit": 99})
    assert r.status_code == 400
    assert "holder_concurrency_limit" in r.json()["detail"]["fields"]

    r = client.put("/api/settings", json={"credential_valid_days": 99999})
    assert r.status_code == 400

    # 非白名单键
    r = client.put("/api/settings", json={"smtp_host": "evil"})
    assert r.status_code == 400
    assert "不支持" in r.json()["detail"]

    # 合法写入后立即可读，且业务助手返回新值
    r = client.put(
        "/api/settings",
        json={
            "lease_ttl_minutes": 45,
            "offer_window_seconds": 90,
            "holder_concurrency_limit": 3,
            "allow_apply_when_suspect": True,
            "credential_valid_days": 180,
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["lease_ttl_minutes"] == 45
    assert data["offer_window_seconds"] == 90
    assert data["holder_concurrency_limit"] == 3
    assert data["allow_apply_when_suspect"] is True
    assert data["credential_valid_days"] == 180
    assert "location_rule" in data

    s = db.session()
    try:
        assert get_runtime_value(s, "lease_ttl_minutes") == 45
        assert get_runtime_value(s, "offer_window_seconds") == 90
        assert get_runtime_value(s, "holder_concurrency_limit") == 3
        assert get_runtime_value(s, "allow_apply_when_suspect") is True
    finally:
        s.close()

    g = client.get("/api/settings")
    assert g.status_code == 200
    assert g.json()["lease_ttl_minutes"] == 45


def test_delete_product_type_ref_gate_and_success(db):
    client, _ = _admin_client(db)

    # 新建无引用机型 → 可删
    code = "TMP_DEL_OK"
    r = client.post("/api/product-types", json={"type_code": code, "display_name": code})
    assert r.status_code == 200, r.text
    r = client.delete(f"/api/product-types/{code}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True
    s = db.session()
    try:
        assert s.get(ProductType, code) is None
    finally:
        s.close()

    # 有设备引用 → 409
    code2 = "TMP_DEL_DEV"
    r = client.post("/api/product-types", json={"type_code": code2, "display_name": code2})
    assert r.status_code == 200
    # seed 里没有 TMP_DEL_DEV 的探测模型，直接插 Device 行模拟引用
    s = db.session()
    try:
        s.add(
            Device(
                device_id="dev_tmpdel01",
                display_name="tmp",
                model_class=code2,
                owner="owner@corp.com",
                status="available",
                health_level="L3",
                created_at=utcnow(),
            )
        )
        s.commit()
    finally:
        s.close()

    r = client.delete(f"/api/product-types/{code2}")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "设备 1" in detail
    assert "请先迁移" in detail

    # 清设备后，活动排队仍挡删除
    s = db.session()
    try:
        s.query(Device).filter(Device.device_id == "dev_tmpdel01").delete()
        p = s.query(Principal).filter(Principal.email == "user01@corp.com").first()
        now = utcnow()
        s.add(
            QueueEntry(
                entry_id="T-tmpdel01",
                principal_id=p.id,
                target_type="product",
                target_code=code2,
                state=QueueState.QUEUED,
                priority=0,
                joined_at=now,
                created_at=now,
            )
        )
        s.commit()
    finally:
        s.close()

    r = client.delete(f"/api/product-types/{code2}")
    assert r.status_code == 409
    assert "活动排队 1" in r.json()["detail"]

    s = db.session()
    try:
        s.query(QueueEntry).filter(QueueEntry.entry_id == "T-tmpdel01").delete()
        s.commit()
    finally:
        s.close()

    r = client.delete(f"/api/product-types/{code2}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True


def test_delete_product_type_blocks_active_lease(db):
    client, _ = _admin_client(db)
    code = "TMP_DEL_LEASE"
    r = client.post("/api/product-types", json={"type_code": code, "display_name": code})
    assert r.status_code == 200

    s = db.session()
    try:
        s.add(
            Device(
                device_id="dev_tmplease",
                display_name="tmp",
                model_class=code,
                owner="owner@corp.com",
                status="occupied",
                health_level="L3",
                created_at=utcnow(),
            )
        )
        p = s.query(Principal).filter(Principal.email == "user02@corp.com").first()
        s.add(
            Lease(
                lease_id=new_lease_id(),
                device_id="dev_tmplease",
                principal_id=p.id,
                ttl_minutes=30,
                state=LeaseState.GRANTED,
                created_at=utcnow(),
            )
        )
        s.commit()
    finally:
        s.close()

    r = client.delete(f"/api/product-types/{code}")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "设备 1" in detail
    assert "活动租约 1" in detail


def test_batch_generate_manual_holders_no_email(db):
    client, _ = _admin_client(db)

    # 仅 manual_holders，无邮箱 → 成功
    r = client.post(
        "/api/credentials/batch-generate",
        json={
            "manual_holders": [{"name": "仅姓名甲", "note": "线下拷贝"}, {"name": "仅姓名乙"}],
            "role": "user",
            "note": "batch-note",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["count"] == 2
    assert len(data["results"]) == 2
    for item in data["results"]:
        assert item["has_email"] is False
        assert item["email"] in (None, "")
        assert item["name"] in ("仅姓名甲", "仅姓名乙")
        assert item["token"].startswith("rr_")
        # 不暴露占位邮箱
        assert item["email"] is None or "@local" not in str(item["email"])

    # 库里 Principal 用占位 email，但 name 正确
    s = db.session()
    try:
        principals = (
            s.query(Principal)
            .filter(Principal.email.like("manual-%@local"))
            .order_by(Principal.id)
            .all()
        )
        assert len(principals) >= 2
        names = {p.name for p in principals}
        assert "仅姓名甲" in names and "仅姓名乙" in names
        for p in principals:
            assert p.email.endswith("@local")
            assert p.email.startswith("manual-")
        # 凭据已入库
        creds = (
            s.query(Credential)
            .filter(Credential.principal_id.in_([p.id for p in principals[-2:]]))
            .all()
        )
        assert len(creds) >= 2
    finally:
        s.close()

    # batch-send 跳过无邮箱条目
    batch_id = data["batch_id"]
    r = client.post("/api/credentials/batch-send", json={"batch_id": batch_id})
    assert r.status_code == 200, r.text
    send = r.json()
    assert send["count"] == 2
    assert all(x.get("skipped") for x in send["results"])
    assert all(not x.get("sent") for x in send["results"])

    # 空请求仍 400
    r = client.post("/api/credentials/batch-generate", json={"role": "user"})
    assert r.status_code == 400
    assert "至少" in r.json()["detail"]
