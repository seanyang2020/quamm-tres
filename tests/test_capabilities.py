"""Phase 1：统一能力发现（capabilities）契约与行为测试。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import event

from app.constants import DeviceStatus, HealthLevel
from app.domain import capabilities as cap_svc
from app.domain import serial as serial_svc
from app.domain import wirelog
from app.migrations import migrate
from app.models import (
    Device,
    DeviceSerialPort,
    SerialAction,
    Setting,
    WireLogPortBinding,
)
from app.utils import utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _add_device(
    session,
    device_id: str,
    *,
    ip: str | None = "10.0.0.1",
    status: str = DeviceStatus.AVAILABLE,
    name: str | None = None,
) -> Device:
    now = utcnow()
    d = Device(
        device_id=device_id,
        display_name=name or device_id,
        model_class=None,
        status=status,
        health_level=HealthLevel.L3,
        owner="t",
        source="mock",
        ip=ip,
        adb_port=5555,
        created_at=now,
        last_seen_at=now,
    )
    session.add(d)
    session.flush()
    return d


def _add_node(session, name: str = "cap-n"):
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
    com_name: str | None = None,
    com_key: str | None = None,
) -> WireLogPortBinding:
    b = WireLogPortBinding(
        node_id=node_id,
        slot=slot,
        bound_device_id=device_id,
        endpoint=endpoint,
        health_state=health_state,
        reach_ok=reach_ok,
        baud=115200,
        com_name=com_name or f"COM{slot}",
        com_key=com_key or (
            endpoint.replace("tcp://", "") if endpoint else None
        ),
        tcp_port=int((endpoint or "tcp://x:0").rsplit(":", 1)[-1]) if endpoint else None,
    )
    session.add(b)
    session.flush()
    return b


def _add_power_port(
    session,
    device_id: str,
    *,
    slot: int = 1,
    endpoint: str = "tcp://127.0.0.1:9210",
    with_actions: bool = True,
) -> DeviceSerialPort:
    port = DeviceSerialPort(
        device_id=device_id,
        slot=slot,
        role="power",
        label="PWR",
        transport="tcp",
        endpoint=endpoint,
        baudrate=115200,
        enabled=True,
        created_at=utcnow(),
    )
    session.add(port)
    session.flush()
    if with_actions:
        for key, steps in (
            ("power_on", [{"hex": "A1"}]),
            ("power_off", [{"hex": "A0"}]),
        ):
            session.add(SerialAction(
                port_id=port.id,
                action_key=key,
                kind="power",
                steps=steps,
                inter_step_ms=10,
                enabled=True,
                sort=0,
            ))
    session.flush()
    return port


def test_adb_only_device(session):
    _add_device(session, "dev_adb", ip="10.0.0.8")
    session.commit()
    snap = cap_svc.build_snapshot(session, "dev_adb")
    assert snap["summary"]["adb"] == "available"
    assert snap["summary"]["serial"] == "unavailable"
    assert snap["summary"]["power"] == "unavailable"
    assert snap["summary"]["mode"] == "unavailable"
    adb = next(i for i in snap["items"] if i["kind"] == "adb")
    assert adb["status"] == "available"


def test_binding_only_serial(session):
    _add_device(session, "dev_bo")
    p = _add_node(session, "bo-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_bo",
                 endpoint="tcp://172.16.20.174:9200", com_name="ttyUSB0",
                 com_key="172.16.20.174:9200")
    session.commit()

    snap = cap_svc.build_snapshot(session, "dev_bo")
    assert snap["summary"]["serial"] == "available"
    assert snap["summary"]["power"] == "unavailable"
    assert snap["summary"]["mode"] == "unavailable"
    io = [i for i in snap["items"] if i["kind"] == "serial.io"]
    assert len(io) == 1
    assert io[0]["source"] == "observed"
    assert io[0]["actions"] == []
    assert io[0]["constraints"]["read_only"] is True

    caps = serial_svc.caps(session, "dev_bo")
    assert caps["ports"] == []
    assert len(caps["observed_channels"]) == 1
    assert caps["observed_channels"][0]["endpoint"] == "tcp://172.16.20.174:9200"


def test_declared_power_healthy_binding(session):
    _add_device(session, "dev_pw")
    p = _add_node(session, "pw-n")
    ep = "tcp://127.0.0.1:9301"
    _add_binding(session, node_id=p["node_id"], device_id="dev_pw", endpoint=ep)
    _add_power_port(session, "dev_pw", endpoint=ep)
    session.commit()

    snap = cap_svc.build_snapshot(session, "dev_pw")
    assert snap["summary"]["power"] == "available"
    power = next(i for i in snap["items"] if i["kind"] == "power")
    keys = {a["action_key"] for a in power["actions"]}
    assert {"power_on", "power_off", "power_cycle"} <= keys


def test_declared_power_binding_offline(session):
    _add_device(session, "dev_poff")
    p = _add_node(session, "poff-n")
    ep = "tcp://127.0.0.1:9302"
    _add_binding(session, node_id=p["node_id"], device_id="dev_poff",
                 endpoint=ep, health_state="offline")
    _add_power_port(session, "dev_poff", endpoint=ep)
    session.commit()

    snap = cap_svc.build_snapshot(session, "dev_poff")
    power = next(i for i in snap["items"] if i["kind"] == "power")
    assert power["status"] == "unavailable"
    assert "ENDPOINT_OFFLINE" in power["status_reasons"]


def test_declared_port_no_binding(session):
    _add_device(session, "dev_nb")
    _add_power_port(session, "dev_nb", endpoint="tcp://127.0.0.1:9303")
    session.commit()

    snap = cap_svc.build_snapshot(session, "dev_nb")
    io = next(i for i in snap["items"] if i["kind"] == "serial.io")
    assert io["status"] == "unavailable"
    assert "NO_BINDING" in io["status_reasons"]
    power = next(i for i in snap["items"] if i["kind"] == "power")
    assert power["status"] == "unavailable"
    assert "NO_BINDING" in power["status_reasons"]


def test_device_offline_marks_serial(session):
    _add_device(session, "dev_off", status=DeviceStatus.OFFLINE)
    p = _add_node(session, "off-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_off")
    session.commit()

    snap = cap_svc.build_snapshot(session, "dev_off")
    io = next(i for i in snap["items"] if i["kind"] == "serial.io")
    assert io["status"] in ("degraded", "unavailable")
    assert "DEVICE_OFFLINE" in io["status_reasons"]


def test_revision_stable_and_changes_on_binding(session):
    _add_device(session, "dev_rev")
    p = _add_node(session, "rev-n")
    b = _add_binding(session, node_id=p["node_id"], device_id="dev_rev",
                     endpoint="tcp://127.0.0.1:9401")
    session.commit()

    r1 = cap_svc.build_snapshot(session, "dev_rev")["revision"]
    r2 = cap_svc.build_snapshot(session, "dev_rev")["revision"]
    assert r1 == r2
    assert r1.startswith("cap1:")

    b.endpoint = "tcp://127.0.0.1:9402"
    b.com_key = "127.0.0.1:9402"
    session.commit()
    r3 = cap_svc.build_snapshot(session, "dev_rev")["revision"]
    assert r3 != r1

    b.health_state = "suspect"
    session.commit()
    r4 = cap_svc.build_snapshot(session, "dev_rev")["revision"]
    assert r4 != r3


def test_revision_sensitive_to_steps_and_inter_step_ms(session):
    _add_device(session, "dev_steps")
    p = _add_node(session, "steps-n")
    ep = "tcp://127.0.0.1:9501"
    _add_binding(session, node_id=p["node_id"], device_id="dev_steps", endpoint=ep)
    port = _add_power_port(session, "dev_steps", endpoint=ep)
    session.commit()

    r1 = cap_svc.build_snapshot(session, "dev_steps")["revision"]
    act = (
        session.query(SerialAction)
        .filter_by(port_id=port.id, action_key="power_on")
        .one()
    )
    act.steps = [{"hex": "FF"}]
    session.commit()
    r2 = cap_svc.build_snapshot(session, "dev_steps")["revision"]
    assert r2 != r1

    act.inter_step_ms = 99
    session.commit()
    r3 = cap_svc.build_snapshot(session, "dev_steps")["revision"]
    assert r3 != r2


def test_capability_json_no_secrets(session):
    _add_device(session, "dev_sec")
    p = _add_node(session, "sec-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_sec")
    session.commit()
    snap = cap_svc.build_snapshot(session, "dev_sec")
    blob = json.dumps(snap, ensure_ascii=False).lower()
    for bad in ("local_token", '"token"', "password", "passwd"):
        assert bad not in blob


def test_ports_declared_only_binding_in_observed(session):
    _add_device(session, "dev_mix")
    p = _add_node(session, "mix-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_mix",
                 endpoint="tcp://127.0.0.1:9601", slot=1)
    # declared on different endpoint → ports has 1 declared; observed has binding
    _add_power_port(session, "dev_mix", endpoint="tcp://127.0.0.1:9602", slot=2)
    session.commit()

    caps = serial_svc.caps(session, "dev_mix")
    assert len(caps["ports"]) == 1
    assert caps["ports"][0]["slot"] == 2
    assert "capability_id" in caps["ports"][0]
    assert any(c["endpoint"] == "tcp://127.0.0.1:9601" for c in caps["observed_channels"])


def test_build_summaries_batch_no_n_plus_1(session):
    ids = []
    for i in range(5):
        did = f"dev_sum{i}"
        ids.append(did)
        _add_device(session, did, ip=f"10.0.1.{i}")
        p = _add_node(session, f"sum-n{i}")
        _add_binding(session, node_id=p["node_id"], device_id=did,
                     endpoint=f"tcp://127.0.0.1:{9700 + i}", slot=1)
    session.commit()

    engine = session.get_bind()
    queries: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        summaries = cap_svc.build_summaries(session, ids)
    finally:
        event.remove(engine, "before_cursor_execute", _count)

    assert len(summaries) == 5
    for did, s in summaries.items():
        assert "revision" in s
        assert "actions" not in s
        assert s["serial"] == "available"
    # O(1) 批次：远小于逐设备 N×k；5 台设备查询次数应有上界
    assert len(queries) <= 12


def test_capabilities_flag_off(session):
    _add_device(session, "dev_flag")
    p = _add_node(session, "flag-n")
    _add_binding(session, node_id=p["node_id"], device_id="dev_flag")
    session.add(Setting(key="capabilities_v1", value="false", updated_at=utcnow()))
    session.commit()

    assert cap_svc.is_enabled(session) is False
    from app.mcp.server import _serial_device_info, _serial_list_devices

    info = _serial_device_info(session, "dev_flag")
    assert "capabilities" not in info
    assert "capability_revision" not in info
    assert "serial_openable" in info
    assert info["serial_openable"] is True

    rows = _serial_list_devices(session, claimable_only=False)
    row = next(r for r in rows if r["device_id"] == "dev_flag")
    assert "capabilities_summary" not in row
    assert "serial_openable" in row


def test_binding_level_shared_endpoint(session):
    _add_device(session, "dev_s1", ip="10.0.2.1")
    _add_device(session, "dev_s2", ip="10.0.2.2")
    p1 = _add_node(session, "sh-a")
    p2 = _add_node(session, "sh-b")
    ep = "tcp://127.0.0.1:9801"
    _add_binding(session, node_id=p1["node_id"], device_id="dev_s1",
                 endpoint=ep, com_key="127.0.0.1:9801", slot=1)
    _add_binding(session, node_id=p2["node_id"], device_id="dev_s2",
                 endpoint=ep, com_key="127.0.0.1:9801", slot=1)
    session.commit()

    snap = cap_svc.build_snapshot(session, "dev_s1")
    io = next(i for i in snap["items"] if i["kind"] == "serial.io")
    assert io["constraints"]["shared_endpoint"] is True
    assert "SHARED_ENDPOINT" in io["status_reasons"]


def test_revision_excludes_timestamps(session):
    _add_device(session, "dev_ts")
    p = _add_node(session, "ts-n")
    b = _add_binding(session, node_id=p["node_id"], device_id="dev_ts")
    session.commit()
    r1 = cap_svc.build_snapshot(session, "dev_ts")["revision"]
    b.last_checked_at = utcnow()
    b.reach_checked_at = utcnow()
    session.commit()
    r2 = cap_svc.build_snapshot(session, "dev_ts")["revision"]
    assert r1 == r2


@pytest.mark.parametrize(
    "health_state,expect_status,expect_reason",
    [
        ("open_failed", "unavailable", "ENDPOINT_OPEN_FAILED"),
        ("stale", "unavailable", "ENDPOINT_OFFLINE"),
        ("superseded", "unavailable", "ENDPOINT_OFFLINE"),
        ("no_response", "degraded", "HEALTH_SUSPECT"),
    ],
)
def test_binding_health_state_mapping(
    session, health_state, expect_status, expect_reason
):
    """真实 binding health_state 不得默认 available。"""
    did = f"dev_hs_{health_state}"
    _add_device(session, did, ip=f"10.8.0.{abs(hash(health_state)) % 200}")
    p = _add_node(session, f"hs-{health_state}")
    _add_binding(
        session,
        node_id=p["node_id"],
        device_id=did,
        endpoint=f"tcp://127.0.0.1:{9100 + (abs(hash(health_state)) % 50)}",
        health_state=health_state,
        reach_ok=True,
    )
    session.commit()

    snap = cap_svc.build_snapshot(session, did)
    io = next(i for i in snap["items"] if i["kind"] == "serial.io")
    assert io["status"] == expect_status
    assert expect_reason in io["status_reasons"]
    assert snap["summary"]["serial"] == expect_status


def test_unknown_health_state_not_available(session):
    _add_device(session, "dev_hs_unk")
    p = _add_node(session, "hs-unk")
    _add_binding(
        session, node_id=p["node_id"], device_id="dev_hs_unk",
        health_state="weird_future_state", reach_ok=True,
    )
    session.commit()
    io = next(
        i for i in cap_svc.build_snapshot(session, "dev_hs_unk")["items"]
        if i["kind"] == "serial.io"
    )
    assert io["status"] in ("unknown", "degraded")
    assert io["status"] != "available"


def test_revision_changes_on_health_state(session):
    _add_device(session, "dev_hs_rev")
    p = _add_node(session, "hs-rev")
    b = _add_binding(
        session, node_id=p["node_id"], device_id="dev_hs_rev",
        health_state="online",
    )
    session.commit()
    r1 = cap_svc.build_snapshot(session, "dev_hs_rev")["revision"]
    b.health_state = "open_failed"
    session.commit()
    r2 = cap_svc.build_snapshot(session, "dev_hs_rev")["revision"]
    assert r2 != r1
    b.health_state = "no_response"
    session.commit()
    r3 = cap_svc.build_snapshot(session, "dev_hs_rev")["revision"]
    assert r3 != r2
    assert r3 != r1
