"""PT3b：GET /api/devices/{id}/capabilities 只读接口。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.constants import DeviceStatus, HealthLevel
from app.migrations import migrate
from app.models import Credential, Device, Principal, Setting
from app.utils import utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


@pytest.fixture
def client(db):
    s = db.session()
    try:
        migrate(s)
    finally:
        s.close()
    from app.main import app
    return TestClient(app)


@pytest.fixture
def admin_cookie(db):
    s = db.session()
    try:
        cred = s.query(Credential).filter_by(role="admin").first()
        if cred is None:
            cred = s.query(Credential).first()
            assert cred is not None
            p = s.get(Principal, cred.principal_id)
            p.is_admin = True
            cred.role = "admin"
            s.commit()
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}
    finally:
        s.close()


def _add_device(session, device_id: str = "dev_cap_api") -> Device:
    now = utcnow()
    d = Device(
        device_id=device_id,
        display_name=device_id,
        model_class=None,
        status=DeviceStatus.AVAILABLE,
        health_level=HealthLevel.L3,
        owner="t",
        source="mock",
        ip="10.0.9.1",
        adb_port=5555,
        created_at=now,
        last_seen_at=now,
    )
    session.add(d)
    session.commit()
    return d


def test_capabilities_enabled(client, session, admin_cookie):
    _add_device(session, "dev_cap_on")
    r = client.get("/api/devices/dev_cap_on/capabilities", cookies=admin_cookie)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "items" in body
    assert "revision" in body
    assert isinstance(body["items"], list)
    # 至少有 adb 项
    kinds = {i.get("kind") for i in body["items"]}
    assert "adb" in kinds
    # enabled 关闭分支才显式带 false；开启时返回 snapshot（可不含 enabled）
    assert body.get("enabled") is not False


def test_capabilities_disabled(client, session, admin_cookie):
    _add_device(session, "dev_cap_off")
    session.add(Setting(key="capabilities_v1", value="false", updated_at=utcnow()))
    session.commit()
    r = client.get("/api/devices/dev_cap_off/capabilities", cookies=admin_cookie)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"enabled": False, "items": [], "revision": 0}


def test_capabilities_unknown_device_404(client, admin_cookie):
    r = client.get("/api/devices/dev_does_not_exist/capabilities", cookies=admin_cookie)
    assert r.status_code == 404


def test_capabilities_requires_login(client):
    """未登录不得读能力快照（与 powerres 管理面约定一致）。"""
    r = client.get("/api/devices/dev_cap_on/capabilities")
    assert r.status_code == 401


def test_list_devices_includes_serial_power_n(client, session, admin_cookie):
    """GET /devices 行附带 serial.power_n（PT6a 轻量字段）。"""
    from app.models import DeviceSerialPort

    _add_device(session, "dev_list_serial")
    session.add(
        DeviceSerialPort(
            device_id="dev_list_serial",
            slot=1,
            role="power",
            transport="tcp",
            endpoint="tcp://127.0.0.1:9262",
            enabled=True,
            created_at=utcnow(),
        )
    )
    session.commit()

    r = client.get("/api/devices", cookies=admin_cookie)
    assert r.status_code == 200, r.text
    rows = r.json()
    row = next(x for x in rows if x["device_id"] == "dev_list_serial")
    assert "serial" in row
    assert isinstance(row["serial"], dict)
    assert row["serial"].get("power_n") == 1
    assert row["serial"].get("log_n") == 0
    # 既有布尔键仍在
    assert "power" in row["serial"] and "log" in row["serial"]
