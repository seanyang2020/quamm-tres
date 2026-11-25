"""t979：GET /api/devices 派生字段 serial_openable / serial_ports。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.constants import DeviceStatus, HealthLevel
from app.domain import catalog, wirelog
from app.migrations import migrate
from app.models import Credential, Device, Principal, WireLogPortBinding
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


def _add_device(session, device_id: str, *, name: str | None = None) -> Device:
    now = utcnow()
    d = Device(
        device_id=device_id,
        display_name=name or device_id,
        model_class=None,
        status=DeviceStatus.AVAILABLE,
        health_level=HealthLevel.L3,
        owner="t",
        source="mock",
        created_at=now,
    )
    session.add(d)
    session.flush()
    return d


def _add_node(session, name: str = "so-n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _add_binding(
    session,
    *,
    node_id: str,
    device_id: str,
    slot: int = 1,
    endpoint: str | None = "tcp://127.0.0.1:9210",
    health_state: str = "online",
    reach_ok: bool | None = True,
) -> WireLogPortBinding:
    b = WireLogPortBinding(
        node_id=node_id,
        slot=slot,
        bound_device_id=device_id,
        endpoint=endpoint,
        health_state=health_state,
        reach_ok=reach_ok,
        baud=115200,
        com_name=f"COM{slot}",
    )
    session.add(b)
    session.flush()
    return b


def test_serial_openable_true_when_reachable(session):
    _add_device(session, "dev_ok")
    p = _add_node(session, "ok-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_ok",
                 health_state="online", reach_ok=True)
    session.commit()

    rows = catalog.list_devices(session, for_agent=False)
    row = next(r for r in rows if r["device_id"] == "dev_ok")
    assert row["serial_openable"] is True
    assert row["serial_ports"] == 1


def test_serial_openable_false_when_health_offline(session):
    _add_device(session, "dev_off")
    p = _add_node(session, "off-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_off",
                 health_state="offline", reach_ok=True)
    session.commit()

    rows = catalog.list_devices(session, for_agent=False)
    row = next(r for r in rows if r["device_id"] == "dev_off")
    assert row["serial_openable"] is False
    assert row["serial_ports"] == 1


def test_serial_openable_false_when_reach_ok_false(session):
    _add_device(session, "dev_ur")
    p = _add_node(session, "ur-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_ur",
                 health_state="online", reach_ok=False)
    session.commit()

    rows = catalog.list_devices(session, for_agent=False)
    row = next(r for r in rows if r["device_id"] == "dev_ur")
    assert row["serial_openable"] is False
    assert row["serial_ports"] == 1


def test_serial_openable_false_when_unbound(session):
    _add_device(session, "dev_nb")
    session.commit()

    rows = catalog.list_devices(session, for_agent=False)
    row = next(r for r in rows if r["device_id"] == "dev_nb")
    assert row["serial_openable"] is False
    assert row["serial_ports"] == 0


def test_serial_openable_true_when_reach_untested(session):
    """reach_ok=None（未测）且 health 正常 → 视为可用。"""
    _add_device(session, "dev_ut")
    p = _add_node(session, "ut-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_ut",
                 health_state="online", reach_ok=None)
    session.commit()

    rows = catalog.list_devices(session, for_agent=False)
    row = next(r for r in rows if r["device_id"] == "dev_ut")
    assert row["serial_openable"] is True
    assert row["serial_ports"] == 1


def test_serial_openable_false_when_no_listen(session):
    _add_device(session, "dev_nl")
    p = _add_node(session, "nl-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_nl",
                 health_state="no_listen", reach_ok=None)
    session.commit()

    rows = catalog.list_devices(session, for_agent=False)
    row = next(r for r in rows if r["device_id"] == "dev_nl")
    assert row["serial_openable"] is False


def test_serial_openable_false_when_endpoint_empty(session):
    _add_device(session, "dev_ep")
    p = _add_node(session, "ep-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_ep",
                 endpoint=None, health_state="online", reach_ok=True)
    session.commit()

    rows = catalog.list_devices(session, for_agent=False)
    row = next(r for r in rows if r["device_id"] == "dev_ep")
    assert row["serial_openable"] is False
    assert row["serial_ports"] == 1


def test_api_devices_includes_serial_fields(client, db, admin_cookie, session):
    _add_device(session, "dev_api")
    p = _add_node(session, "api-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_api")
    session.commit()

    r = client.get("/api/devices", cookies=admin_cookie)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert isinstance(rows, list)
    row = next(x for x in rows if x["device_id"] == "dev_api")
    assert "serial_openable" in row and "serial_ports" in row
    assert row["serial_openable"] is True
    assert row["serial_ports"] == 1
    # 既有字段仍在
    assert "device_id" in row and "status" in row


def test_binding_only_openable_vs_serial_available(session):
    """binding-only：serial_openable 与 capabilities.summary.serial 一致；
    serial.available 仍表示 declared DeviceSerialPort（保持 false）。"""
    from app.domain import capabilities as cap_svc
    from app.domain import serial as serial_svc

    _add_device(session, "dev_bo_av")
    p = _add_node(session, "bo-av-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_bo_av",
                 health_state="online", reach_ok=True)
    session.commit()

    rows = catalog.list_devices(session, for_agent=False)
    row = next(r for r in rows if r["device_id"] == "dev_bo_av")
    assert row["serial_openable"] is True

    snap = cap_svc.build_snapshot(session, "dev_bo_av")
    assert snap["summary"]["serial"] == "available"

    summary = serial_svc.serial_summary(session, "dev_bo_av")
    assert summary["available"] is False
