"""v0.27 设备锁定 / 占用者邮箱 / 编译服务器 token 隔离与去重 单测。"""
from __future__ import annotations

import pytest

from app.domain import build_servers as buildsvc
from app.models import Principal
from app.domain import catalog, leasing
from app.mcp.server import res_apply
from tests.conftest import register_device
from tests.mock_device import MockDevice


# ---------- 设备锁定 ----------

def test_lock_hides_from_claimable_pool(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "402-A-01", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        catalog.device_update(s, did, "user01@corp.com", False,
                              {"locked": True, "locked_reason": "送修"})
        s.close()

        s = db.session()
        row = next(x for x in catalog.list_devices(s, for_agent=False, limit=999)
                   if x["device_id"] == did)
        assert row["locked"] is True
        assert row["claimable"] is False
        assert row["locked_reason"] == "送修"
        pool = [x["device_id"] for x in
                catalog.list_devices(s, for_agent=True, claimable_only=True, limit=999)]
        s.close()
        assert did not in pool
    finally:
        dev.stop()


def test_lock_rejects_explicit_apply(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "402-A-02", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        catalog.device_update(s, did, "user01@corp.com", False,
                              {"locked": True, "locked_reason": "人工检查"})
        s.close()

        r = db.call("user01@corp.com", res_apply, target=did, purpose="验证锁定")
        assert r.get("error") == "DEVICE_LOCKED", r
        assert r.get("device_id") == did
        assert "人工检查" in str(r.get("reason"))
    finally:
        dev.stop()


def test_non_owner_cannot_lock(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "402-A-03", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        with pytest.raises(catalog.DomainError) as ei:
            catalog.device_update(s, did, "user02@corp.com", False, {"locked": True})
        assert ei.value.code == "FORBIDDEN"
        s.close()
    finally:
        dev.stop()


def test_unlock_clears_fields(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "402-A-04", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        catalog.device_update(s, did, "user01@corp.com", False,
                              {"locked": True, "locked_reason": "临时"})
        out = catalog.device_update(s, did, "user01@corp.com", False, {"locked": False})
        s.close()
        assert out["locked"] is False
        assert out["locked_by"] is None
        assert out["locked_reason"] is None
    finally:
        dev.stop()


# ---------- 占用者邮箱 ----------

def test_holder_email_in_web_view(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "402-A-05", "owner@corp.com")
        did = reg["device_id"]
        r = db.call("user01@corp.com", res_apply, target=did, purpose="占位")
        assert r["state"] == "GRANTED", r

        s = db.session()
        web = next(x for x in catalog.list_devices(s, for_agent=False, limit=999)
                   if x["device_id"] == did)
        agent_view = next(x for x in catalog.list_devices(s, for_agent=True, limit=999)
                          if x["device_id"] == did)
        s.close()
        assert web["holder_email"] == "user01@corp.com"
        assert agent_view.get("holder_email") is None
    finally:
        dev.stop()


# ---------- 编译服务器隔离与去重 ----------

def _mk(db, email, name="bs1", host="10.1.1.1", username="u1"):
    s = db.session()
    p = s.query(Principal).filter(Principal.email == email).one()
    out = buildsvc.create_build_server(
        s, actor=email, principal_id=p.id, name=name, host=host, port=22,
        username=username, auth_mode="agent", secret=None,
    )
    s.close()
    return out


def test_build_server_isolated_per_token(db):
    a = _mk(db, "user01@corp.com", name="bs-a", host="10.0.0.1")
    b = _mk(db, "user02@corp.com", name="bs-b", host="10.0.0.2")
    s = db.session()
    p1 = s.query(Principal).filter_by(email="user01@corp.com").one()
    rows = buildsvc.list_build_servers(s, principal_id=p1.id, is_admin=False)
    s.close()
    assert [r["server_id"] for r in rows] == [a["server_id"]]
    assert b["server_id"] not in [r["server_id"] for r in rows]


def test_admin_list_still_isolated_unless_include_all(db):
    a = _mk(db, "user01@corp.com", name="bs-a", host="10.0.0.1")
    s = db.session()
    admin = s.query(Principal).filter_by(is_admin=True).first()
    own = buildsvc.list_build_servers(s, principal_id=admin.id, is_admin=True)
    allrows = buildsvc.list_build_servers(s, principal_id=admin.id, is_admin=True,
                                          include_all=True)
    s.close()
    assert own == [] or own[0]["server_id"] != a["server_id"]
    assert any(r["server_id"] == a["server_id"] for r in allrows)


def test_duplicate_host_hint(db):
    _mk(db, "user01@corp.com", name="bs-a", host="10.9.9.9", username="shared")
    out = _mk(db, "user02@corp.com", name="bs-b", host="10.9.9.9", username="shared")
    assert "dedup_hint" in out, out
    assert "user01@corp.com" in out["dedup_hint"]
    assert out["duplicate_host_owners"][0]["owner"] == "user01@corp.com"
