"""PowerRes 独立资源域：inventory sync / bind / unbind / capabilities 接线。"""
from __future__ import annotations

import pytest

from app.constants import DeviceStatus, HealthLevel
from app.domain import capabilities as cap_svc
from app.domain import powerres
from app.domain import serial as serial_svc
from app.domain import wirelog
from app.domain.catalog import DomainError
from app.migrations import migrate, migrate_serial_action_hex_steps
from app import serial_tcp
from app import web_auth
from app.models import (
    Credential,
    Device,
    DeviceSerialPort,
    PowerResBinding,
    PowerResInventory,
    Principal,
    SerialAction,
)
from app.utils import utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _add_device(session, device_id: str = "dev_pwr") -> Device:
    now = utcnow()
    d = Device(
        device_id=device_id,
        display_name=device_id,
        model_class=None,
        status=DeviceStatus.AVAILABLE,
        health_level=HealthLevel.L3,
        owner="t",
        source="mock",
        ip="10.0.0.50",
        adb_port=5555,
        created_at=now,
        last_seen_at=now,
    )
    session.add(d)
    session.flush()
    return d


def _add_node(session, name: str = "pwr-n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _fill_slots(session, device_id: str, n: int = 16, role: str = "log") -> None:
    now = utcnow()
    for slot in range(1, n + 1):
        session.add(DeviceSerialPort(
            device_id=device_id,
            slot=slot,
            role=role,
            transport="tcp",
            endpoint=f"tcp://127.0.0.1:{9000 + slot}",
            enabled=True,
            created_at=now,
        ))
    session.flush()


# ---- 1) sync_inventory 幂等 + 设备消失清理 --------------------------------

def test_sync_inventory_idempotent_and_prune(session):
    node = _add_node(session)
    nid = node["node_id"]

    devices = [{
        "id": "relay1",
        "name": "BULL-GN315",
        "com": "COM24",
        "channels": [
            {"ch": 2, "endpoint": "tcp://127.0.0.1:9262", "state": "on"},
            {"ch": 3, "endpoint": "tcp://127.0.0.1:9263", "state": "off"},
        ],
    }]
    r1 = powerres.sync_inventory(session, nid, devices)
    assert r1["upserted"] == 2
    assert r1["removed"] == 0
    assert len(r1["inventory"]) == 2

    # 幂等再 sync 同样 payload
    r2 = powerres.sync_inventory(session, nid, devices)
    assert r2["upserted"] == 2
    assert r2["removed"] == 0
    rows = session.query(PowerResInventory).filter_by(node_id=nid).all()
    assert len(rows) == 2

    # 设备/通道消失 → 清理
    slim = [{
        "id": "relay1",
        "name": "BULL-GN315",
        "com": "COM24",
        "channels": [
            {"ch": 2, "endpoint": "tcp://127.0.0.1:9262", "state": "on"},
        ],
    }]
    r3 = powerres.sync_inventory(session, nid, slim)
    assert r3["removed"] == 1
    assert len(r3["inventory"]) == 1
    assert r3["inventory"][0]["channel"] == 2

    # 整设备消失
    r4 = powerres.sync_inventory(session, nid, [])
    assert r4["removed"] == 1
    assert r4["inventory"] == []


# ---- 2) bind 成功建立 power 端口 ------------------------------------------

def test_bind_creates_power_port(session):
    node = _add_node(session)
    dev = _add_device(session, "dev_bind")
    session.commit()

    ep = "tcp://127.0.0.1:9262"
    result = powerres.bind(
        session,
        node["node_id"],
        "relay1",
        2,
        ep,
        device_id=dev.device_id,
        label="relay1 CH2",
    )
    port_info = result["port"]
    assert port_info["role"] == "power"
    assert port_info["enabled"] is True
    assert port_info["endpoint"] == ep
    assert 1 <= port_info["slot"] <= 16

    port = (
        session.query(DeviceSerialPort)
        .filter_by(device_id=dev.device_id, slot=port_info["slot"])
        .one()
    )
    assert port.role == "power"
    assert port.transport == "tcp"
    assert port.channel == "ch2"
    assert bool(port.enabled) is True

    # 不与既有非 power 端口冲突：先占 slot1
    session2_node = _add_node(session, "pwr-n2")
    dev2 = _add_device(session, "dev_bind2")
    session.add(DeviceSerialPort(
        device_id=dev2.device_id,
        slot=1,
        role="log",
        transport="tcp",
        endpoint="tcp://127.0.0.1:9101",
        enabled=True,
        created_at=utcnow(),
    ))
    session.commit()
    r2 = powerres.bind(
        session,
        session2_node["node_id"],
        "relay1",
        3,
        "tcp://127.0.0.1:9263",
        device_id=dev2.device_id,
        slot=1,  # 已被 log 占用 → 应跳过改用其他 slot
    )
    assert r2["port"]["slot"] != 1
    log_port = (
        session.query(DeviceSerialPort)
        .filter_by(device_id=dev2.device_id, slot=1)
        .one()
    )
    assert log_port.role == "log"


# ---- 3) 重复 bind 复用 slot、不产生重复行 ---------------------------------

def test_rebind_reuses_slot(session):
    node = _add_node(session)
    dev = _add_device(session, "dev_re")
    session.commit()
    nid = node["node_id"]

    r1 = powerres.bind(
        session, nid, "relay1", 2, "tcp://127.0.0.1:9262",
        device_id=dev.device_id,
    )
    slot1 = r1["port"]["slot"]
    binding_id = r1["binding"]["id"]

    r2 = powerres.bind(
        session, nid, "relay1", 2, "tcp://127.0.0.1:9262",
        device_id=dev.device_id,
        label="updated",
    )
    assert r2["port"]["slot"] == slot1
    assert r2["binding"]["id"] == binding_id
    assert r2["binding"]["label"] == "updated"

    cnt = (
        session.query(PowerResBinding)
        .filter_by(node_id=nid, power_dev="relay1", channel=2)
        .count()
    )
    assert cnt == 1
    ports = (
        session.query(DeviceSerialPort)
        .filter_by(device_id=dev.device_id, role="power")
        .all()
    )
    assert len(ports) == 1


# ---- 4) unbind：端口 enabled=0、binding 删除 -------------------------------

def test_unbind_disables_port_deletes_binding(session):
    node = _add_node(session)
    dev = _add_device(session, "dev_ub")
    session.commit()
    nid = node["node_id"]

    r = powerres.bind(
        session, nid, "relay1", 2, "tcp://127.0.0.1:9262",
        device_id=dev.device_id,
    )
    slot = r["port"]["slot"]

    out = powerres.unbind(session, nid, "relay1", 2)
    assert out["port"]["enabled"] is False

    assert (
        session.query(PowerResBinding)
        .filter_by(node_id=nid, power_dev="relay1", channel=2)
        .count()
    ) == 0
    port = (
        session.query(DeviceSerialPort)
        .filter_by(device_id=dev.device_id, slot=slot)
        .one()
    )
    assert bool(port.enabled) is False
    assert port.role == "power"  # 行保留


# ---- 5) slot 用尽：报错且不覆盖既有 role ----------------------------------

def test_bind_no_free_slot_does_not_overwrite(session):
    node = _add_node(session)
    dev = _add_device(session, "dev_full")
    _fill_slots(session, dev.device_id, n=16, role="log")
    session.commit()

    before = {
        (p.slot, p.role, p.endpoint)
        for p in session.query(DeviceSerialPort).filter_by(device_id=dev.device_id).all()
    }
    with pytest.raises(DomainError) as ei:
        powerres.bind(
            session,
            node["node_id"],
            "relay1",
            2,
            "tcp://127.0.0.1:9262",
            device_id=dev.device_id,
        )
    assert ei.value.code == "POWERRES_NO_SLOT"
    assert "no free slot" in ei.value.message

    after = {
        (p.slot, p.role, p.endpoint)
        for p in session.query(DeviceSerialPort).filter_by(device_id=dev.device_id).all()
    }
    assert before == after
    assert session.query(PowerResBinding).count() == 0


# ---- 6) bind 后 capabilities power 不再 NO_BINDING ------------------------

def test_bind_clears_capabilities_no_binding(session):
    node = _add_node(session)
    dev = _add_device(session, "dev_cap")
    session.commit()

    ep = "tcp://127.0.0.1:9262"
    powerres.bind(
        session,
        node["node_id"],
        "relay1",
        2,
        ep,
        device_id=dev.device_id,
    )

    snap = cap_svc.build_snapshot(session, dev.device_id)
    power_items = [i for i in snap["items"] if i["kind"] == "power"]
    assert len(power_items) == 1
    power = power_items[0]
    assert "NO_BINDING" not in power["status_reasons"]
    assert power["binding"] is not None
    assert power["binding"].get("source") == "powerres"
    assert power["status"] in ("available", "degraded")
    assert snap["summary"]["power"] in ("available", "degraded")


def test_bind_rejects_empty_endpoint(session):
    node = _add_node(session)
    dev = _add_device(session, "dev_bad_ep")
    session.commit()
    with pytest.raises(DomainError) as ei:
        powerres.bind(
            session,
            node["node_id"],
            "relay1",
            2,
            "",
            device_id=dev.device_id,
        )
    assert ei.value.code == "POWERRES_BAD_ENDPOINT"


# ---- 7) overview：只含有 inventory 或绑定的节点 ----------------------------

def test_overview_only_nodes_with_power(session):
    n1 = _add_node(session, "pwr-has")
    n2 = _add_node(session, "pwr-empty")
    n3 = _add_node(session, "pwr-bound-only")
    session.commit()

    powerres.sync_inventory(session, n1["node_id"], [{
        "id": "relay1",
        "name": "BULL",
        "com": "COM24",
        "channels": [
            {"ch": 1, "endpoint": "tcp://127.0.0.1:9201"},
            {"ch": 2, "endpoint": "tcp://127.0.0.1:9202"},
        ],
    }])

    # n3：仅有绑定、无 inventory，也应出现在 overview
    dev = _add_device(session, "dev_ov")
    session.commit()
    powerres.bind(
        session, n3["node_id"], "relayX", 1, "tcp://127.0.0.1:9301",
        device_id=dev.device_id,
    )

    ov = powerres.overview(session)
    assert ov["ok"] is True
    ids = {n["node_id"] for n in ov["nodes"]}
    assert n1["node_id"] in ids
    assert n3["node_id"] in ids
    assert n2["node_id"] not in ids  # 无 power 资源、无绑定

    row1 = next(n for n in ov["nodes"] if n["node_id"] == n1["node_id"])
    assert row1["devices"] == 1
    assert row1["channels"] == 2
    assert row1["bound"] == 0
    assert row1["display_name"]

    row3 = next(n for n in ov["nodes"] if n["node_id"] == n3["node_id"])
    assert row3["bound"] == 1
    assert row3["channels"] == 0


# ---- 8) 手工 upsert：幂等 + 返回完整 inventory -----------------------------

def test_manual_upsert_inventory_idempotent(session):
    node = _add_node(session, "pwr-man")
    session.commit()
    nid = node["node_id"]

    r1 = powerres.upsert_inventory_item(
        session, nid, "relayM", 3,
        name="手工继电器",
        com="COM99",
        endpoint="tcp://10.0.0.1:9403",
        state="off",
    )
    assert len(r1) == 1
    assert r1[0]["power_dev"] == "relayM"
    assert r1[0]["channel"] == 3
    assert r1[0]["endpoint"] == "tcp://10.0.0.1:9403"
    assert r1[0]["name"] == "手工继电器"
    assert r1[0]["bound"] is False

    r2 = powerres.upsert_inventory_item(
        session, nid, "relayM", 3,
        name="改名",
        com="COM99",
        endpoint="tcp://10.0.0.1:9403",
        state="on",
    )
    assert len(r2) == 1
    assert r2[0]["name"] == "改名"
    assert r2[0]["state"] == "on"
    cnt = (
        session.query(PowerResInventory)
        .filter_by(node_id=nid, power_dev="relayM", channel=3)
        .count()
    )
    assert cnt == 1

    # overview 能看到该节点
    ov = powerres.overview(session)
    assert any(n["node_id"] == nid for n in ov["nodes"])


# ---- 9) 手工 upsert 校验 + DELETE ----------------------------------------

def test_manual_upsert_validation_and_delete(session):
    node = _add_node(session, "pwr-del")
    session.commit()
    nid = node["node_id"]

    with pytest.raises(DomainError) as ei:
        powerres.upsert_inventory_item(session, nid, "", 1, endpoint="tcp://127.0.0.1:1")
    assert ei.value.code == "POWERRES_BAD_PAYLOAD"

    with pytest.raises(DomainError) as ei2:
        powerres.upsert_inventory_item(
            session, nid, "relayBad", "x",
            endpoint="tcp://127.0.0.1:1",
        )
    assert ei2.value.code == "POWERRES_BAD_PAYLOAD"

    with pytest.raises(DomainError) as ei3:
        powerres.upsert_inventory_item(
            session, nid, "relayBad", 1,
            endpoint="not-an-endpoint",
        )
    assert ei3.value.code == "POWERRES_BAD_ENDPOINT"

    powerres.upsert_inventory_item(
        session, nid, "relayD", 5,
        endpoint="tcp://127.0.0.1:9505",
    )
    out = powerres.delete_inventory_item(session, nid, "relayD", 5)
    assert out["ok"] is True
    assert out["inventory"] == []
    assert (
        session.query(PowerResInventory)
        .filter_by(node_id=nid, power_dev="relayD", channel=5)
        .count()
    ) == 0

    with pytest.raises(DomainError) as ei4:
        powerres.delete_inventory_item(session, nid, "relayD", 5)
    assert ei4.value.code == "NOT_FOUND"


# ---- PT3a: steps data/encoding + hex 兼容 + power_cycle + 迁移 ---------------

def test_bind_default_actions_use_data_encoding(session):
    """bind 后默认 power_on/off steps 为 data+encoding；power_cycle 空 steps。"""
    node = _add_node(session, "pwr-steps")
    dev = _add_device(session, "dev_steps")
    session.commit()

    r = powerres.bind(
        session, node["node_id"], "relay1", 1, "tcp://127.0.0.1:9261",
        device_id=dev.device_id,
    )
    port = (
        session.query(DeviceSerialPort)
        .filter_by(device_id=dev.device_id, slot=r["port"]["slot"])
        .one()
    )
    acts = {
        a.action_key: a
        for a in session.query(SerialAction).filter_by(port_id=port.id).all()
    }
    assert "power_on" in acts and "power_off" in acts and "power_cycle" in acts
    for key in ("power_on", "power_off"):
        steps = acts[key].steps
        assert isinstance(steps, list) and len(steps) == 1
        assert "data" in steps[0]
        assert steps[0].get("encoding") == "hex"
        assert "hex" not in steps[0]
        # 继电器帧：A0 | ch | op | checksum
        assert str(steps[0]["data"]).upper().startswith("A001")
    assert acts["power_cycle"].steps == [] or acts["power_cycle"].steps is None


def test_legacy_hex_steps_still_execute(session, monkeypatch):
    """旧 {"hex":...} 动作经 normalize 兼容后仍可执行；发出的字节与 hex 一致。"""
    node = _add_node(session, "pwr-legacy")
    dev = _add_device(session, "dev_legacy")
    session.commit()
    r = powerres.bind(
        session, node["node_id"], "relay1", 2, "tcp://127.0.0.1:9262",
        device_id=dev.device_id,
    )
    port = (
        session.query(DeviceSerialPort)
        .filter_by(device_id=dev.device_id, slot=r["port"]["slot"])
        .one()
    )
    on_act = (
        session.query(SerialAction)
        .filter_by(port_id=port.id, action_key="power_on")
        .one()
    )
    legacy_hex = "A00201A3"
    on_act.steps = [{"hex": legacy_hex}]
    session.commit()

    # 直接校验 normalize 兼容
    norm = serial_tcp.normalize_steps([{"hex": legacy_hex}])
    assert norm[0]["data"] == legacy_hex
    assert norm[0]["encoding"] == "hex"
    expected_bytes = serial_tcp.encode_data(legacy_hex, "hex")

    captured: list[bytes] = []

    def fake_execute(target, steps, **kwargs):
        normed = serial_tcp.normalize_steps(steps)
        for st in normed:
            enc = st["encoding"] or kwargs.get("default_encoding") or "hex"
            captured.append(serial_tcp.encode_data(st["data"], enc))
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

    out = serial_svc.run_action(
        session, dev.device_id, "power_on", by="test:legacy",
    )
    assert out["result"] == "ok"
    assert captured == [expected_bytes]


def test_power_cycle_hold_ms_trail(session, monkeypatch):
    """power_cycle：off → hold(hold_ms) → on；on 失败带 phase=power_on。"""
    node = _add_node(session, "pwr-cycle")
    dev = _add_device(session, "dev_cycle")
    session.commit()
    powerres.bind(
        session, node["node_id"], "relay1", 1, "tcp://127.0.0.1:9271",
        device_id=dev.device_id,
    )

    trail: list[str] = []
    sleep_ms: list[int] = []
    call_n = {"n": 0}

    def fake_execute(target, steps, **kwargs):
        call_n["n"] += 1
        phase = "power_off" if call_n["n"] == 1 else "power_on"
        trail.append(phase)
        # 第二次（on）失败
        ok = call_n["n"] == 1
        return serial_tcp.ActionResult(
            ok=ok,
            transport="tcp",
            target=str(target),
            steps=[],
            response_tail="",
            duration_ms=1,
            error=None if ok else "on boom",
            error_code=None if ok else "SERIAL_IO",
        )

    def fake_sleep(sec):
        sleep_ms.append(int(round(sec * 1000)))
        trail.append(f"hold:{sleep_ms[-1]}")

    monkeypatch.setattr(serial_tcp, "execute_steps", fake_execute)
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)
    monkeypatch.setattr(serial_svc.time, "sleep", fake_sleep)

    out = serial_svc.run_action(
        session, dev.device_id, "power_cycle", by="test:cycle",
        hold_ms=1500, confirm=True,
    )
    assert out["result"] == "fail"
    assert out["phase"] == "power_on"
    assert out.get("hold_ms") == 1500
    assert trail == ["power_off", "hold:1500", "power_on"]

    # 成功轨迹：再跑一次全成功
    trail.clear()
    sleep_ms.clear()
    call_n["n"] = 0

    def fake_ok(target, steps, **kwargs):
        call_n["n"] += 1
        trail.append("power_off" if call_n["n"] == 1 else "power_on")
        return serial_tcp.ActionResult(
            ok=True, transport="tcp", target=str(target),
            steps=[], response_tail="", duration_ms=1,
        )

    monkeypatch.setattr(serial_tcp, "execute_steps", fake_ok)
    out2 = serial_svc.run_action(
        session, dev.device_id, "power_cycle", by="test:cycle",
        hold_ms=500, confirm=True,
    )
    assert out2["result"] == "ok"
    assert out2.get("hold_ms") == 500
    assert trail == ["power_off", "hold:500", "power_on"]


def test_migrate_serial_action_hex_steps_idempotent(session):
    """轻量迁移：hex→data 幂等；不在 migrate() 自动跑。"""
    node = _add_node(session, "pwr-mig")
    dev = _add_device(session, "dev_mig")
    session.commit()
    r = powerres.bind(
        session, node["node_id"], "relay1", 3, "tcp://127.0.0.1:9283",
        device_id=dev.device_id,
    )
    port = (
        session.query(DeviceSerialPort)
        .filter_by(device_id=dev.device_id, slot=r["port"]["slot"])
        .one()
    )
    off = (
        session.query(SerialAction)
        .filter_by(port_id=port.id, action_key="power_off")
        .one()
    )
    off.steps = [{"hex": "A00300A3", "delay_ms": 10}]
    # 并存：以 data 为准
    on = (
        session.query(SerialAction)
        .filter_by(port_id=port.id, action_key="power_on")
        .one()
    )
    on.steps = [{"data": "A00301A4", "hex": "DEAD", "encoding": "hex"}]
    session.commit()

    r1 = migrate_serial_action_hex_steps(session)
    assert r1["ok"] is True
    assert r1["rewritten"] >= 2

    session.refresh(off)
    session.refresh(on)
    assert off.steps[0]["data"] == "A00300A3"
    assert off.steps[0]["encoding"] == "hex"
    assert "hex" not in off.steps[0]
    assert off.steps[0].get("delay_ms") == 10
    assert on.steps[0]["data"] == "A00301A4"
    assert "hex" not in on.steps[0]

    r2 = migrate_serial_action_hex_steps(session)
    assert r2["rewritten"] == 0
    assert r2["unchanged"] == r2["scanned"]


def test_normalize_steps_hex_compat_both_fields(session):
    """hex 与 data 并存时以 data 为准。"""
    out = serial_tcp.normalize_steps([
        {"data": "AABB", "hex": "FFFF", "encoding": "hex"},
    ])
    assert out[0]["data"] == "AABB"
    assert out[0]["encoding"] == "hex"


# ---- PT5a: location/driver 补全 + 权限收紧 -----------------------------------

def _api_client(db):
    s = db.session()
    try:
        migrate(s)
    finally:
        s.close()
    from starlette.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _cookie(db, admin: bool):
    s = db.session()
    try:
        if admin:
            cred = s.query(Credential).filter_by(role="admin").first()
            if cred is None:
                cred = s.query(Credential).first()
                assert cred is not None
                p = s.get(Principal, cred.principal_id)
                p.is_admin = True
                cred.role = "admin"
                s.commit()
        else:
            cred = s.query(Credential).filter_by(role="user").first()
            if cred is None:
                cred = s.query(Credential).first()
                assert cred is not None
                p = s.get(Principal, cred.principal_id)
                p.is_admin = False
                cred.role = "user"
                s.commit()
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}
    finally:
        s.close()


def test_pt5a_sync_with_location_driver(session):
    """sync 带/不带 location+driver 均落库；旧 hub 不传不影响。"""
    node = _add_node(session, "pwr-loc")
    nid = node["node_id"]
    r1 = powerres.sync_inventory(session, nid, [{
        "id": "relayL",
        "name": "BULL-GN315",
        "com": "COM29",
        "location": "三楼实验室 A 台",
        "driver": "bull-gn315",
        "channels": [{"ch": 1, "endpoint": "tcp://127.0.0.1:9601"}],
    }])
    assert r1["upserted"] == 1
    row = r1["inventory"][0]
    assert row["location"] == "三楼实验室 A 台"
    assert row["driver"] == "bull-gn315"

    # 幂等：同 payload 再 sync
    r2 = powerres.sync_inventory(session, nid, [{
        "id": "relayL",
        "location": "三楼实验室 A 台",
        "driver": "bull-gn315",
        "channels": [{"ch": 1, "endpoint": "tcp://127.0.0.1:9601"}],
    }])
    assert r2["upserted"] == 1 and r2["removed"] == 0

    # 不带 location/driver 的旧 hub → None
    r3 = powerres.sync_inventory(session, nid, [{
        "id": "relayL",
        "channels": [{"ch": 2, "endpoint": "tcp://127.0.0.1:9602"}],
    }])
    assert r3["inventory"][0]["location"] is None
    assert r3["inventory"][0]["driver"] is None


def test_pt5a_sync_overlong_payload_rejected(session):
    """location/driver 超长 → POWERRES_BAD_PAYLOAD（不静默截断）。"""
    node = _add_node(session, "pwr-long")
    nid = node["node_id"]
    with pytest.raises(DomainError) as ei:
        powerres.sync_inventory(session, nid, [{
            "id": "relayLong",
            "location": "x" * 65,
            "channels": [{"ch": 1, "endpoint": "tcp://127.0.0.1:9601"}],
        }])
    assert ei.value.code == "POWERRES_BAD_PAYLOAD"
    with pytest.raises(DomainError) as ei2:
        powerres.sync_inventory(session, nid, [{
            "id": "relayLong",
            "driver": "y" * 33,
            "channels": [{"ch": 1, "endpoint": "tcp://127.0.0.1:9601"}],
        }])
    assert ei2.value.code == "POWERRES_BAD_PAYLOAD"
    with pytest.raises(DomainError) as ei3:
        powerres.upsert_inventory_item(
            session, nid, "relayLong", 1,
            endpoint="tcp://127.0.0.1:9601",
            location="z" * 65,
        )
    assert ei3.value.code == "POWERRES_BAD_PAYLOAD"


def test_pt5a_bind_and_bindings_carry_location_driver(session):
    """bind 返回与 bindings 行带 location/driver（从 inventory 取）。"""
    node = _add_node(session, "pwr-bmeta")
    nid = node["node_id"]
    dev = _add_device(session, "dev_meta")
    session.commit()
    powerres.sync_inventory(session, nid, [{
        "id": "relayB",
        "location": "二楼机柜",
        "driver": "bull-gn315",
        "channels": [{"ch": 3, "endpoint": "tcp://127.0.0.1:9603"}],
    }])
    r = powerres.bind(
        session, nid, "relayB", 3, "tcp://127.0.0.1:9603",
        device_id=dev.device_id,
    )
    assert r["binding"]["location"] == "二楼机柜"
    assert r["binding"]["driver"] == "bull-gn315"
    bl = powerres.list_bindings(session, nid)
    assert bl[0]["location"] == "二楼机柜"
    assert bl[0]["driver"] == "bull-gn315"


def test_pt5a_api_write_requires_admin(session, db):
    """bind/unbind/inventory 写：非 admin 403，admin 正常；GET 类非 admin 200。"""
    from app import web_auth
    from app.models import Credential, Principal

    node = _add_node(session, "pwr-api")
    nid = node["node_id"]
    dev = _add_device(session, "dev_api")
    session.commit()

    user_cookie = _cookie(db, admin=False)
    admin_cookie = _cookie(db, admin=True)
    client = _api_client(db)

    # 非 admin → 403
    for method, url, kw in (
        ("post", "/api/powerres/bind", {"json": {
            "node_id": nid, "power_dev": "relayA", "channel": 1,
            "endpoint": "tcp://127.0.0.1:9701", "device_id": dev.device_id,
        }}),
        ("post", "/api/powerres/unbind", {"json": {
            "node_id": nid, "power_dev": "relayA", "channel": 1,
        }}),
        ("post", "/api/powerres/inventory", {"json": {
            "node_id": nid, "power_dev": "relayA", "channel": 1,
            "endpoint": "tcp://127.0.0.1:9701",
        }}),
        ("delete", "/api/powerres/inventory", {"params": {
            "node_id": nid, "power_dev": "relayA", "channel": 1,
        }}),
    ):
        r = getattr(client, method)(url, cookies=user_cookie, **kw)
        assert r.status_code == 403, (method, url, r.status_code, r.text)

    # 读接口非 admin 200
    r = client.get("/api/powerres/overview", cookies=user_cookie)
    assert r.status_code == 200
    r = client.get("/api/powerres/inventory", params={"node_id": nid},
                   cookies=user_cookie)
    assert r.status_code == 200

    # admin 正常 bind（带 location/driver）
    r = client.post("/api/powerres/inventory", cookies=admin_cookie, json={
        "node_id": nid, "power_dev": "relayA", "channel": 1,
        "endpoint": "tcp://127.0.0.1:9701",
        "location": "A 台", "driver": "bull-gn315",
    })
    assert r.status_code == 200, r.text
    assert r.json()[0]["location"] == "A 台"
    r = client.post("/api/powerres/bind", cookies=admin_cookie, json={
        "node_id": nid, "power_dev": "relayA", "channel": 1,
        "endpoint": "tcp://127.0.0.1:9701", "device_id": dev.device_id,
    })
    assert r.status_code == 200, r.text
    assert r.json()["binding"]["location"] == "A 台"
    # admin unbind 清理
    r = client.post("/api/powerres/unbind", cookies=admin_cookie, json={
        "node_id": nid, "power_dev": "relayA", "channel": 1,
    })
    assert r.status_code == 200, r.text


# ---- PT8-tres: restart_hold_ms sync / 读接口 --------------------------------

def test_pt8_sync_restart_hold_ms(session):
    """sync 写入 restart_hold_ms；非法值置空；读接口带出该字段。"""
    node = _add_node(session, "pwr-hold-sync")
    nid = node["node_id"]

    r1 = powerres.sync_inventory(session, nid, [{
        "id": "relayH",
        "name": "BULL-GN315",
        "com": "COM30",
        "channels": [
            {"ch": 1, "endpoint": "tcp://127.0.0.1:9611", "state": "on",
             "restart_hold_ms": 1200},
            {"ch": 2, "endpoint": "tcp://127.0.0.1:9612", "state": "off",
             "restart_hold_ms": 0},
        ],
    }])
    assert r1["upserted"] == 2
    by_ch = {row["channel"]: row for row in r1["inventory"]}
    assert by_ch[1]["restart_hold_ms"] == 1200
    assert by_ch[2]["restart_hold_ms"] == 0

    # 非法值 → NULL
    r2 = powerres.sync_inventory(session, nid, [{
        "id": "relayH",
        "channels": [
            {"ch": 1, "endpoint": "tcp://127.0.0.1:9611",
             "restart_hold_ms": -1},
            {"ch": 2, "endpoint": "tcp://127.0.0.1:9612",
             "restart_hold_ms": "bad"},
            {"ch": 3, "endpoint": "tcp://127.0.0.1:9613"},
        ],
    }])
    by_ch = {row["channel"]: row for row in r2["inventory"]}
    assert by_ch[1]["restart_hold_ms"] is None
    assert by_ch[2]["restart_hold_ms"] is None
    assert by_ch[3]["restart_hold_ms"] is None

    # list_inventory 读接口同样带字段
    listed = powerres.list_inventory(session, nid)
    assert all("restart_hold_ms" in row for row in listed)

    # 手工登记不带该字段 → NULL（非注册通道）
    powerres.upsert_inventory_item(
        session, nid, "relayManual", 1,
        endpoint="tcp://127.0.0.1:9619", state="on",
    )
    manual = [
        r for r in powerres.list_inventory(session, nid)
        if r["power_dev"] == "relayManual"
    ][0]
    assert manual["restart_hold_ms"] is None
