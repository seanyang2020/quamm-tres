"""retired 设备立即释放串口绑定；存量清理幂等。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.constants import DeviceStatus, HealthLevel
from app.domain import catalog, wirelog
from app.migrations import migrate
from app.models import AuditEvent, Device, WireLogPortBinding
from app.utils import new_device_id, utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _add_device(session, *, status=DeviceStatus.AVAILABLE, name=None) -> Device:
    now = utcnow()
    d = Device(
        device_id=new_device_id(),
        display_name=name or "retire-dev",
        status=status,
        health_level=HealthLevel.L3,
        owner="t",
        source="serial",
        register_channel="manual",
        created_at=now,
        last_seen_at=now,
        position_code="302-R9-01",
    )
    if status == DeviceStatus.RETIRED:
        d.retired_at = now
        d.position_code = None
    session.add(d)
    session.flush()
    return d


def _add_binding(session, *, device_id: str, node_id: str, slot: int = 1) -> WireLogPortBinding:
    now = utcnow()
    b = WireLogPortBinding(
        node_id=node_id,
        slot=slot,
        bound_device_id=device_id,
        endpoint=f"tcp://127.0.0.1:{9200 + slot}",
        com_name=f"COM{slot}",
        health_state="online",
        baud=115200,
        allocated_by="auto:test",
        allocated_at=now,
        allocated_expire=now + timedelta(hours=1),
    )
    session.add(b)
    session.flush()
    return b


def test_retire_releases_serial_binding(session):
    d = _add_device(session, name="to-retire")
    p = wirelog.provision_host(session, display_name="retire-n", operator="t")
    b = _add_binding(session, device_id=d.device_id, node_id=p["node_id"])
    binding_id = b.id
    device_id = d.device_id
    session.commit()

    out = catalog.device_unregister(
        session, device_id, actor_email="admin@corp.com", is_admin=True
    )
    assert out["status"] == DeviceStatus.RETIRED

    d2 = session.get(Device, device_id)
    assert d2 is not None
    assert d2.status == DeviceStatus.RETIRED

    b2 = session.get(WireLogPortBinding, binding_id)
    assert b2 is not None
    assert b2.bound_device_id is None
    assert b2.allocated_by is None
    assert b2.allocated_at is None
    assert b2.allocated_expire is None

    ev = (
        session.query(AuditEvent)
        .filter(AuditEvent.device_id == device_id, AuditEvent.action == "unregister")
        .order_by(AuditEvent.event_id.desc())
        .first()
    )
    assert ev is not None
    assert ev.detail is not None
    released = ev.detail.get("released_bindings") or []
    assert len(released) == 1
    assert released[0]["previous_device_id"] == device_id
    assert released[0]["binding_id"] == binding_id
    assert released[0]["node_id"] == p["node_id"]
    assert released[0]["com_name"] == "COM1"
    assert ev.detail.get("released_position") == "302-R9-01"


def test_release_bindings_of_retired_devices_idempotent(session):
    d = _add_device(session, status=DeviceStatus.RETIRED, name="already-retired")
    p = wirelog.provision_host(session, display_name="cleanup-n", operator="t")
    b = _add_binding(session, device_id=d.device_id, node_id=p["node_id"], slot=2)
    binding_id = b.id
    device_id = d.device_id
    session.commit()

    r1 = catalog.release_bindings_of_retired_devices(session, actor_email="admin@corp.com")
    assert r1["released"] == 1
    assert any(x["device_id"] == device_id for x in r1["devices"])

    b2 = session.get(WireLogPortBinding, binding_id)
    assert b2 is not None
    assert b2.bound_device_id is None
    assert b2.allocated_by is None

    ev = (
        session.query(AuditEvent)
        .filter(
            AuditEvent.device_id == device_id,
            AuditEvent.action == "retired_bindings_released",
        )
        .first()
    )
    assert ev is not None
    assert ev.detail and ev.detail.get("released_bindings")
    assert ev.detail["released_bindings"][0]["previous_device_id"] == device_id

    r2 = catalog.release_bindings_of_retired_devices(session, actor_email="admin@corp.com")
    assert r2["released"] == 0
    assert r2["devices"] == []
