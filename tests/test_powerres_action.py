"""PT7a：PowerRes 通道级动作 API（admin 直控未绑定通道）。"""
from __future__ import annotations

import pytest

from app.constants import DeviceStatus, HealthLevel
from app.domain import powerres
from app.domain import serial as serial_svc
from app.domain import wirelog
from app.domain.catalog import DomainError
from app.migrations import migrate
from app import serial_tcp
from app import web_auth
from app.models import (
    Credential,
    Device,
    PowerResBinding,
    PowerResInventory,
    Principal,
)
from app.utils import utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _add_device(session, device_id: str = "dev_ch") -> Device:
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


def _add_node(session, name: str = "pwr-act"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _seed_inv(session, nid, power_dev="relay1", channel=1,
              endpoint="tcp://127.0.0.1:9801", state=None):
    powerres.upsert_inventory_item(
        session, nid, power_dev, channel,
        endpoint=endpoint, state=state,
    )
    return (
        session.query(PowerResInventory)
        .filter_by(node_id=nid, power_dev=power_dev, channel=channel)
        .one()
    )


def _ok_result(response_tail: str = ""):
    return serial_tcp.ActionResult(
        ok=True, transport="tcp", target="tcp://x",
        steps=[], response_tail=response_tail, duration_ms=1,
    )


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


# ---- 1) 帧构造 --------------------------------------------------------------

def test_relay_frame_hex_ch1_on_off():
    """继电器帧 A0|ch|op|sum8，hex 大写无空格。

    CH1 on:  op=0x01 → sum=(0xA0+1+1)&0xFF=0xA2 → A00101A2
    CH1 off: op=0x00 → sum=(0xA0+1+0)&0xFF=0xA1 → A00100A1
    （注意不是 A00101A1；off 的 op 字节是 00 不是 01。）
    """
    assert powerres._relay_frame_hex(1, 0x01) == "A00101A2"
    assert powerres._relay_frame_hex(1, 0x00) == "A00100A1"
    assert powerres._relay_frame_hex(1, 0x05) == "A00105A6"  # query


# ---- 2) power_cycle 顺序 off→hold→on ---------------------------------------

def test_channel_power_cycle_order(session, monkeypatch):
    node = _add_node(session, "pwr-cyc")
    nid = node["node_id"]
    _seed_inv(session, nid, channel=2, endpoint="tcp://127.0.0.1:9802")

    trail: list[str] = []
    sleep_ms: list[int] = []
    frames: list[str] = []
    call_n = {"n": 0}

    def fake_sleep(sec):
        sleep_ms.append(int(round(sec * 1000)))
        trail.append(f"hold:{sleep_ms[-1]}")

    def fake_execute(target, steps, **kwargs):
        call_n["n"] += 1
        hx = steps[0]["data"]
        frames.append(hx)
        if call_n["n"] == 1:
            assert hx == powerres._relay_frame_hex(2, 0x00)
            trail.append("power_off")
        else:
            assert hx == powerres._relay_frame_hex(2, 0x01)
            trail.append("power_on")
        return _ok_result()

    monkeypatch.setattr(serial_tcp, "execute_steps", fake_execute)
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)
    monkeypatch.setattr(powerres.time, "sleep", fake_sleep)

    out = powerres.channel_action(
        session, nid, "relay1", 2, "power_cycle",
        by="web:admin@t", hold_ms=1500,
    )
    assert out["result"] == "ok"
    assert out["hold_ms"] == 1500
    assert out["state"] == "on"
    assert trail == ["power_off", "hold:1500", "power_on"]
    assert frames == [
        powerres._relay_frame_hex(2, 0x00),
        powerres._relay_frame_hex(2, 0x01),
    ]


# ---- 3) hold 夹紧 -----------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    (0, 500),
    (499, 500),
    (500, 500),
    (30000, 30000),
    (30001, 30000),
])
def test_channel_hold_ms_clamped(session, monkeypatch, raw, expected):
    node = _add_node(session, f"pwr-hold-{raw}")
    nid = node["node_id"]
    _seed_inv(session, nid, channel=1, endpoint="tcp://127.0.0.1:9810")

    sleep_ms: list[int] = []
    monkeypatch.setattr(serial_tcp, "execute_steps", lambda *a, **k: _ok_result())
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)
    monkeypatch.setattr(powerres.time, "sleep", lambda s: sleep_ms.append(int(round(s * 1000))))

    out = powerres.channel_action(
        session, nid, "relay1", 1, "power_cycle",
        by="web:admin@t", hold_ms=raw,
    )
    assert out["result"] == "ok"
    assert out["hold_ms"] == expected
    assert sleep_ms == [expected]
    # 与 serial._clamp_hold_ms 同口径
    assert serial_svc._clamp_hold_ms(raw) == expected


# ---- 4) 未绑定可执行；已绑 enabled=False → POWER_DISABLED --------------------

def test_channel_unbound_ok_bound_disabled_rejected(session, monkeypatch):
    node = _add_node(session, "pwr-en")
    nid = node["node_id"]
    dev = _add_device(session, "dev_en")
    session.commit()
    _seed_inv(session, nid, channel=1, endpoint="tcp://127.0.0.1:9821")

    monkeypatch.setattr(serial_tcp, "execute_steps", lambda *a, **k: _ok_result())
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)

    # 未绑定可执行
    out = powerres.channel_action(
        session, nid, "relay1", 1, "power_on", by="web:admin@t",
    )
    assert out["result"] == "ok"
    assert out["state"] == "on"

    # 绑定后停用
    powerres.bind(
        session, nid, "relay1", 1, "tcp://127.0.0.1:9821",
        device_id=dev.device_id,
    )
    b = (
        session.query(PowerResBinding)
        .filter_by(node_id=nid, power_dev="relay1", channel=1)
        .one()
    )
    b.enabled = False
    session.commit()

    with pytest.raises(DomainError) as ei:
        powerres.channel_action(
            session, nid, "relay1", 1, "power_off", by="web:admin@t",
        )
    assert ei.value.code == "POWER_DISABLED"


# ---- 5) 未知通道 / 端点空 ----------------------------------------------------

def test_channel_unknown_and_no_endpoint(session, monkeypatch):
    node = _add_node(session, "pwr-miss")
    nid = node["node_id"]
    monkeypatch.setattr(serial_tcp, "execute_steps", lambda *a, **k: _ok_result())
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)

    with pytest.raises(DomainError) as ei:
        powerres.channel_action(
            session, nid, "nope", 9, "power_on", by="web:admin@t",
        )
    assert ei.value.code == "POWERRES_UNKNOWN_CHANNEL"

    # endpoint 空：直接插行（绕过 upsert 的校验）
    session.add(PowerResInventory(
        node_id=nid, power_dev="relayE", channel=1,
        endpoint=None, updated_at=utcnow(),
    ))
    session.commit()
    with pytest.raises(DomainError) as ei2:
        powerres.channel_action(
            session, nid, "relayE", 1, "power_on", by="web:admin@t",
        )
    assert ei2.value.code == "POWERRES_NO_ENDPOINT"


# ---- 6) API：非 admin 403；admin 正常 ---------------------------------------

def test_api_channel_action_admin_only(session, db, monkeypatch):
    node = _add_node(session, "pwr-api-act")
    nid = node["node_id"]
    _seed_inv(session, nid, channel=1, endpoint="tcp://127.0.0.1:9831")
    session.commit()

    monkeypatch.setattr(serial_tcp, "execute_steps", lambda *a, **k: _ok_result())
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)

    client = _api_client(db)
    user_cookie = _cookie(db, admin=False)
    admin_cookie = _cookie(db, admin=True)
    body = {
        "node_id": nid,
        "power_dev": "relay1",
        "channel": 1,
        "action": "power_on",
    }

    r = client.post("/api/powerres/action", cookies=user_cookie, json=body)
    assert r.status_code == 403, r.text

    r = client.post("/api/powerres/action", cookies=admin_cookie, json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["result"] == "ok"
    assert data["state"] == "on"
    assert data["action"] == "power_on"


def test_channel_bad_action(session):
    node = _add_node(session, "pwr-bad")
    nid = node["node_id"]
    _seed_inv(session, nid, channel=1, endpoint="tcp://127.0.0.1:9841")
    with pytest.raises(DomainError) as ei:
        powerres.channel_action(
            session, nid, "relay1", 1, "power_boom", by="web:admin@t",
        )
    assert ei.value.code == "POWERRES_BAD_ACTION"


def test_channel_query_updates_state(session, monkeypatch):
    node = _add_node(session, "pwr-q")
    nid = node["node_id"]
    _seed_inv(session, nid, channel=1, endpoint="tcp://127.0.0.1:9851", state="off")

    # 应答帧 CH1 on = A00101A2
    monkeypatch.setattr(
        serial_tcp, "execute_steps",
        lambda *a, **k: _ok_result(response_tail="A00101A2"),
    )
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)

    out = powerres.channel_action(
        session, nid, "relay1", 1, "power_query", by="web:admin@t",
    )
    assert out["result"] == "ok"
    assert out["state"] == "on"


# ---- PT8-tres: 已注册 hold 强制 / 手工通道仍夹紧客户端 hold -----------------

def test_pt8_registered_hold_forces_restart_hold_ms(session, monkeypatch):
    """已注册通道（inventory.restart_hold_ms 非空）忽略请求 hold，强制用注册值。"""
    node = _add_node(session, "pwr-reg-hold")
    nid = node["node_id"]
    powerres.sync_inventory(session, nid, [{
        "id": "relay1",
        "channels": [{
            "ch": 1,
            "endpoint": "tcp://127.0.0.1:9861",
            "restart_hold_ms": 1200,
        }],
    }])

    sleep_ms: list[int] = []
    monkeypatch.setattr(serial_tcp, "execute_steps", lambda *a, **k: _ok_result())
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)
    monkeypatch.setattr(powerres.time, "sleep", lambda s: sleep_ms.append(int(round(s * 1000))))

    # 客户端传 8000，应被注册值 1200 覆盖（仍经 _clamp_hold_ms）
    out = powerres.channel_action(
        session, nid, "relay1", 1, "power_cycle",
        by="web:admin@t", hold_ms=8000,
    )
    assert out["result"] == "ok"
    assert out["hold_ms"] == 1200
    assert sleep_ms == [1200]


def test_pt8_manual_channel_accepts_client_hold(session, monkeypatch):
    """手工通道（restart_hold_ms 为空）仍接受客户端 hold 并夹紧。"""
    node = _add_node(session, "pwr-man-hold")
    nid = node["node_id"]
    _seed_inv(session, nid, channel=1, endpoint="tcp://127.0.0.1:9862")
    inv = (
        session.query(PowerResInventory)
        .filter_by(node_id=nid, power_dev="relay1", channel=1)
        .one()
    )
    assert inv.restart_hold_ms is None

    sleep_ms: list[int] = []
    monkeypatch.setattr(serial_tcp, "execute_steps", lambda *a, **k: _ok_result())
    monkeypatch.setattr(serial_tcp, "acquire_wait", lambda *a, **k: None)
    monkeypatch.setattr(serial_tcp, "release", lambda *a, **k: None)
    monkeypatch.setattr(powerres.time, "sleep", lambda s: sleep_ms.append(int(round(s * 1000))))

    out = powerres.channel_action(
        session, nid, "relay1", 1, "power_cycle",
        by="web:admin@t", hold_ms=1500,
    )
    assert out["result"] == "ok"
    assert out["hold_ms"] == 1500
    assert sleep_ms == [1500]

    # 夹紧：低于下限
    sleep_ms.clear()
    out2 = powerres.channel_action(
        session, nid, "relay1", 1, "power_cycle",
        by="web:admin@t", hold_ms=100,
    )
    assert out2["hold_ms"] == 500
    assert sleep_ms == [500]
