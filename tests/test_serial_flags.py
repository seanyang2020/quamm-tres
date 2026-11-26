"""PT6a：serial_flags 新增 power_n / log_n 计数。"""
from __future__ import annotations

import pytest

from app.constants import DeviceStatus, HealthLevel
from app.domain import serial as serial_svc
from app.migrations import migrate
from app.models import Device, DeviceSerialPort, SerialAction
from app.utils import utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _add_device(session, device_id: str) -> Device:
    now = utcnow()
    d = Device(
        device_id=device_id,
        display_name=device_id,
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


def _add_port(
    session,
    *,
    device_id: str,
    slot: int,
    role: str,
    enabled: bool = True,
    with_power_actions: bool = False,
) -> DeviceSerialPort:
    now = utcnow()
    port = DeviceSerialPort(
        device_id=device_id,
        slot=slot,
        role=role,
        transport="tcp",
        endpoint=f"tcp://127.0.0.1:{9100 + slot}",
        enabled=enabled,
        created_at=now,
    )
    session.add(port)
    session.flush()
    if with_power_actions:
        for key in ("power_on", "power_off", "power_cycle"):
            session.add(
                SerialAction(
                    port_id=port.id,
                    action_key=key,
                    kind="power",
                    display_name=key,
                    enabled=True,
                    steps=[],
                )
            )
        session.flush()
    return port


def test_serial_flags_empty_table(session):
    assert serial_svc.serial_flags(session) == {}


def test_serial_flags_power_n_log_n_counts(session):
    _add_device(session, "dev_cnt")
    # 2 个 enabled power（一个带动作、一个不带）→ power_n=2；power bool 仅有动作的为 True
    _add_port(session, device_id="dev_cnt", slot=1, role="power", with_power_actions=True)
    _add_port(session, device_id="dev_cnt", slot=2, role="power", with_power_actions=False)
    # disabled power 不计入
    _add_port(session, device_id="dev_cnt", slot=3, role="power", enabled=False)
    # 2 个 log
    _add_port(session, device_id="dev_cnt", slot=4, role="log")
    _add_port(session, device_id="dev_cnt", slot=5, role="log")
    session.commit()

    flags = serial_svc.serial_flags(session)
    f = flags["dev_cnt"]
    assert f["power_n"] == 2
    assert f["log_n"] == 2
    assert f["power"] is True
    assert f["log"] is True
    assert f["slots"] == 4  # enabled only
    assert "mode" in f


def test_serial_flags_power_n_zero_when_only_log(session):
    _add_device(session, "dev_log_only")
    _add_port(session, device_id="dev_log_only", slot=1, role="log")
    session.commit()

    f = serial_svc.serial_flags(session)["dev_log_only"]
    assert f["power_n"] == 0
    assert f["log_n"] == 1
    assert f["power"] is False
    assert f["log"] is True
