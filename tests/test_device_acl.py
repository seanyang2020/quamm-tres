"""v0.26 设备访问控制 / 编辑 / 共享名单 单测。"""
from __future__ import annotations

import json

import pytest

from app.domain import device_acl
from app.models import Device
from app.mcp.server import res_apply
from tests.conftest import register_device
from tests.mock_device import MockDevice


def _device(scope, emails=None, labels=None, registered_by="owner@corp.com"):
    d = Device()
    d.device_id = "dev_x"
    d.registered_by = registered_by
    d.share_scope = scope
    d.allowed_emails = json.dumps(emails) if emails else None
    d.allowed_agent_labels = json.dumps(labels) if labels else None
    return d


# ---------- 纯判定 ----------

def test_shared_allows_anyone():
    d = _device("shared")
    assert device_acl.can_use_device(None, d, "a@corp.com")
    assert device_acl.can_use_device(None, d, "b@corp.com")


def test_private_only_owner_and_admin():
    d = _device("private")
    assert device_acl.can_use_device(None, d, "owner@corp.com")
    assert device_acl.can_use_device(None, d, "other@corp.com", is_admin=True)
    assert not device_acl.can_use_device(None, d, "other@corp.com")


def test_restricted_email_or_label():
    d = _device("restricted", emails=["ok@corp.com"], labels=["WorkBuddy/1.0"])
    assert device_acl.can_use_device(None, d, "ok@corp.com")
    assert device_acl.can_use_device(None, d, "x@corp.com", "WorkBuddy/1.0")
    assert not device_acl.can_use_device(None, d, "x@corp.com", "Other/1.0")
    assert not device_acl.can_use_device(None, d, "x@corp.com")


def test_serialize_acl():
    d = _device("restricted", emails=["a@corp.com"], labels=["L/1"])
    view = device_acl.serialize_acl(d)
    assert view == {"share_scope": "restricted",
                    "allowed_emails": ["a@corp.com"],
                    "allowed_agent_labels": ["L/1"]}


# ---------- 端到端：编辑 + 共享 ----------

def test_private_device_denies_other_user(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "401-A-01", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        from app.domain.catalog import device_update
        device_update(s, did, "user01@corp.com", False, {"share_scope": "private"})
        s.close()

        r = db.call("user02@corp.com", res_apply, target=did)
        assert r.get("error") == "ACCESS_DENIED", r
    finally:
        dev.stop()


def test_restricted_allows_listed_user(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "401-A-02", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        from app.domain.catalog import device_update
        device_update(s, did, "user01@corp.com", False,
                      {"share_scope": "restricted",
                       "allowed_emails": ["user02@corp.com"]})
        s.close()

        r = db.call("user02@corp.com", res_apply, target=did, purpose="验证")
        assert r["state"] == "GRANTED", r
    finally:
        dev.stop()


def test_non_owner_cannot_edit(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "401-A-03", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        from app.domain.catalog import device_update, DomainError
        with pytest.raises(DomainError) as ei:
            device_update(s, did, "user03@corp.com", False,
                          {"display_name": "hack"})
        assert ei.value.code == "FORBIDDEN"
        s.close()
    finally:
        dev.stop()


def test_owner_can_edit_basic(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "401-A-04", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        from app.domain.catalog import device_update
        out = device_update(s, did, "user01@corp.com", False,
                            {"display_name": "我的设备"})
        s.close()
        assert out["display_name"] == "我的设备"
    finally:
        dev.stop()


def test_owner_cannot_change_identity_fields(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "401-A-05", "owner@corp.com")
        did = reg["device_id"]
        s = db.session()
        from app.domain.catalog import device_update
        out = device_update(s, did, "user01@corp.com", False,
                            {"ip": "9.9.9.9", "model_class": "framex"})
        s.close()
        # 非管理员的身份字段被忽略，保持原值
        assert out["model_class"] != "framex"
    finally:
        dev.stop()
