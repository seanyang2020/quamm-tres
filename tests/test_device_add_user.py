"""v0.54.5：普通用户（role=user）可添加设备；机型字典仍仅管理员维护。

覆盖 Web 三接口 probe / register / discover 对 user 放开、product-types 对 user 仍 403，
以及 admin 四接口回归。
"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.migrations import migrate
from app.models import Credential, Device
from tests.mock_device import MockDevice


@pytest.fixture
def client(db):
    s = db.session()
    try:
        migrate(s)
    finally:
        s.close()
    from app.main import app

    return TestClient(app)


def _cookie_for(db, *, admin: bool):
    s = db.session()
    try:
        if admin:
            cred = s.query(Credential).filter_by(role="admin").first()
            assert cred is not None
        else:
            cred = s.query(Credential).filter_by(role="user").first()
            assert cred is not None
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}, cred.id
    finally:
        s.close()


@pytest.fixture
def mock_dev():
    dev = MockDevice(model="QM102V").start()
    try:
        yield dev
    finally:
        dev.stop()


def _probe(client, cookies, dev):
    r = client.post(
        "/api/devices/probe",
        cookies=cookies,
        json={"ip": dev.ip, "port": dev.port, "source": "mock"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("form_id")
    return body


def _register(client, cookies, form_id, owner="测试责任人"):
    return client.post(
        "/api/devices/register",
        cookies=cookies,
        json={"form_id": form_id, "position_code": "302-R2-05", "owner": owner},
    )


def test_user_can_probe(client, db, mock_dev):
    cookies, _ = _cookie_for(db, admin=False)
    body = _probe(client, cookies, mock_dev)
    assert body["probe"]["ip"] == mock_dev.ip


def test_user_can_register(client, db, mock_dev):
    cookies, _ = _cookie_for(db, admin=False)
    probe = _probe(client, cookies, mock_dev)
    r = _register(client, cookies, probe["form_id"])
    assert r.status_code == 200, r.text
    assert r.json().get("device_id")

    s = db.session()
    try:
        dev = s.query(Device).filter(Device.ip == mock_dev.ip).first()
        assert dev is not None
    finally:
        s.close()


def test_user_can_discover(client, db):
    cookies, _ = _cookie_for(db, admin=False)
    r = client.post(
        "/api/devices/discover",
        cookies=cookies,
        json={"networks": ["127.0.0.1/32"], "timeout": 0.05},
    )
    assert r.status_code == 200, r.text
    assert "devices" in r.json()


def test_user_cannot_create_product_type(client, db):
    cookies, _ = _cookie_for(db, admin=False)
    r = client.post(
        "/api/product-types",
        cookies=cookies,
        json={"type_code": "USER_NEW", "display_name": "USER_NEW"},
    )
    assert r.status_code == 403, r.text


def test_admin_regression_all_four(client, db, mock_dev):
    cookies, _ = _cookie_for(db, admin=True)
    probe = _probe(client, cookies, mock_dev)
    r = _register(client, cookies, probe["form_id"], owner="管理员")
    assert r.status_code == 200, r.text

    d = client.post(
        "/api/devices/discover",
        cookies=cookies,
        json={"networks": ["127.0.0.1/32"], "timeout": 0.05},
    )
    assert d.status_code == 200, d.text

    pt = client.post(
        "/api/product-types",
        cookies=cookies,
        json={"type_code": "ADMIN_NEW", "display_name": "ADMIN_NEW"},
    )
    assert pt.status_code == 200, pt.text
