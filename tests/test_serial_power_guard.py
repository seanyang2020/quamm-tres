"""PT4b：电源动作安全闸 — hold 夹紧 + confirm + binding.enabled + 拒绝审计。"""
from __future__ import annotations

import pytest

from app.constants import DeviceStatus, HealthLevel
from app.domain import powerres
from app.domain import serial as serial_svc
from app.domain import wirelog
from app.domain.catalog import DomainError
from app.migrations import migrate
from app.models import AuditEvent, Device, PowerResBinding
from app import serial_tcp
from app.utils import utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _add_device(session, device_id: str = "dev_guard") -> Device:
    now = utcnow()
    d = Device(
        device_id=device_id,
        display_name=device_id,
        model_class=None,
        status=DeviceStatus.AVAILABLE,
        health_level=HealthLevel.L3,
        owner="t",
        source="mock",
        ip="10.0.0.60",
        adb_port=5555,
        created_at=now,
        last_seen_at=now,
    )
    session.add(d)
    session.flush()
    return d


def _add_node(session, name: str = "guard-n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _bind_power(session, device_id: str, *, node_name: str = "guard-n", ch: int = 1):
    node = _add_node(session, node_name)
    powerres.bind(
        session,
        node["node_id"],
        "relay1",
        ch,
        f"tcp://127.0.0.1:{9200 + ch}",
        device_id=device_id,
    )
    return node


def _patch_serial_ok(monkeypatch):
    def fake_execute(target, steps, **kwargs):
        return serial_tcp.ActionResult(
            ok=True,
            transport="tcp",
            target=str(target),
            steps=[],
            response_tail="",
            duration_ms=1,
        )

    monkeypatch.setattr(serial_tcp, "execute_steps", fake_execute)
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)


# ---- hold 夹紧 ---------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        (0, 500),
        (499, 500),
        (500, 500),
        (30000, 30000),
        (30001, 30000),
    ],
)
def test_hold_ms_clamped(session, monkeypatch, raw, expected):
    """显式 hold_ms 越界夹紧到 [500, 30000]；dry_run 回显夹后值。"""
    dev = _add_device(session, f"dev_hold_{raw}")
    session.commit()
    _bind_power(session, dev.device_id, node_name=f"hold-n-{raw}", ch=1)
    _patch_serial_ok(monkeypatch)

    out = serial_svc.run_action(
        session,
        dev.device_id,
        "power_cycle",
        by="test:hold",
        hold_ms=raw,
        confirm=True,
        dry_run=True,
    )
    assert out["dry_run"] is True
    assert out["hold_ms"] == expected


def test_hold_ms_none_uses_default(session, monkeypatch):
    """hold_ms 缺省仍用 POWER_CYCLE_HOLD_MS，不做下限替换。"""
    dev = _add_device(session, "dev_hold_none")
    session.commit()
    _bind_power(session, dev.device_id, node_name="hold-none", ch=2)
    _patch_serial_ok(monkeypatch)

    out = serial_svc.run_action(
        session,
        dev.device_id,
        "power_cycle",
        by="test:hold",
        hold_ms=None,
        confirm=True,
        dry_run=True,
    )
    assert out["hold_ms"] == serial_svc.POWER_CYCLE_HOLD_MS


# ---- confirm -----------------------------------------------------------------

def test_power_off_requires_confirm(session):
    dev = _add_device(session, "dev_off_noconfirm")
    session.commit()
    _bind_power(session, dev.device_id, node_name="off-nc", ch=1)

    with pytest.raises(DomainError) as ei:
        serial_svc.run_action(
            session, dev.device_id, "power_off", by="test:nc", confirm=False,
        )
    assert ei.value.code == "CONFIRM_REQUIRED"


def test_power_cycle_requires_confirm(session):
    dev = _add_device(session, "dev_cyc_noconfirm")
    session.commit()
    _bind_power(session, dev.device_id, node_name="cyc-nc", ch=1)

    with pytest.raises(DomainError) as ei:
        serial_svc.run_action(
            session, dev.device_id, "power_cycle", by="test:nc", confirm=False,
        )
    assert ei.value.code == "CONFIRM_REQUIRED"


def test_power_on_no_confirm_needed(session, monkeypatch):
    """power_on 不要求 confirm（断电残窗补偿）。"""
    dev = _add_device(session, "dev_on_ok")
    session.commit()
    _bind_power(session, dev.device_id, node_name="on-ok", ch=1)
    _patch_serial_ok(monkeypatch)

    out = serial_svc.run_action(
        session, dev.device_id, "power_on", by="test:on", confirm=False,
    )
    assert out["result"] == "ok"


# ---- binding.enabled ---------------------------------------------------------

def test_power_disabled_when_binding_disabled(session, monkeypatch):
    dev = _add_device(session, "dev_disabled")
    session.commit()
    _bind_power(session, dev.device_id, node_name="dis-n", ch=1)
    b = session.query(PowerResBinding).filter_by(device_id=dev.device_id).one()
    b.enabled = False
    session.commit()
    _patch_serial_ok(monkeypatch)

    with pytest.raises(DomainError) as ei:
        serial_svc.run_action(
            session, dev.device_id, "power_on", by="test:dis", confirm=False,
        )
    assert ei.value.code == "POWER_DISABLED"

    with pytest.raises(DomainError) as ei2:
        serial_svc.run_action(
            session, dev.device_id, "power_off", by="test:dis", confirm=True,
        )
    assert ei2.value.code == "POWER_DISABLED"


def test_no_binding_power_action_allowed(session, monkeypatch):
    """无 PowerResBinding 时按现状放行（老路径不回归）。"""
    from app.models import DeviceSerialPort, SerialAction

    now = utcnow()
    dev = _add_device(session, "dev_nobind")
    port = DeviceSerialPort(
        device_id=dev.device_id,
        slot=1,
        role="power",
        transport="tcp",
        endpoint="tcp://127.0.0.1:9399",
        enabled=True,
        created_at=now,
    )
    session.add(port)
    session.flush()
    session.add(SerialAction(
        port_id=port.id,
        action_key="power_on",
        display_name="开",
        kind="power",
        steps=[{"data": "A00101A2", "encoding": "hex"}],
        enabled=True,
    ))
    session.commit()
    assert session.query(PowerResBinding).filter_by(device_id=dev.device_id).count() == 0
    _patch_serial_ok(monkeypatch)

    out = serial_svc.run_action(
        session, dev.device_id, "power_on", by="test:nobind", confirm=False,
    )
    assert out["result"] == "ok"


# ---- 拒绝写审计 --------------------------------------------------------------

def test_reject_writes_audit(session):
    """confirm/enabled 拒绝时写 result=rejected + error_code 审计。"""
    dev = _add_device(session, "dev_audit_rej")
    session.commit()
    _bind_power(session, dev.device_id, node_name="aud-n", ch=1)

    with pytest.raises(DomainError) as ei:
        serial_svc.run_action(
            session, dev.device_id, "power_off", by="test:audit", confirm=False,
        )
    assert ei.value.code == "CONFIRM_REQUIRED"

    ev = (
        session.query(AuditEvent)
        .filter_by(device_id=dev.device_id, action="serial_action", actor="test:audit")
        .order_by(AuditEvent.event_id.desc())
        .first()
    )
    assert ev is not None
    assert ev.detail.get("result") == "rejected"
    assert ev.detail.get("error_code") == "CONFIRM_REQUIRED"
    assert ev.detail.get("action_key") == "power_off"
