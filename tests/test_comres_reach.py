"""t969：TRES→endpoint 可达性探测（reach_*）与打开前 UNREACHABLE。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.domain import comres, wirelog
from app.migrations import migrate
from app.models import Credential, Principal, WireLogPortBinding


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


def _prov_online(session, name="reach-n"):
    from app.models import WireLogNode

    p = wirelog.provision_host(session, display_name=name, operator="t")
    n = session.get(WireLogNode, p["node_id"])
    n.status = "online"
    n.ip = "127.0.0.1"
    session.commit()
    return p


def _register_port(session, prov, *, com="COM1", endpoint="tcp://127.0.0.1:19999",
                   slot=None):
    ports = [{
        "com": com, "endpoint": endpoint, "health_state": "online",
    }]
    if slot is not None:
        ports[0]["slot"] = slot
    return comres.register(session, prov["node_token"], {
        "node_id": prov["node_id"], "local_ip": "127.0.0.1",
        "ports": ports,
    })


def test_probe_reach_ok(session, monkeypatch):
    p = _prov_online(session, "ok-n")
    _register_port(session, p, com="COM16", endpoint="tcp://127.0.0.1:9210")
    monkeypatch.setattr(comres, "_tcp_probe", lambda *a, **k: (True, None))
    r = comres.probe_reach(session, node_id=p["node_id"], timeout=0.5)
    assert r["checked"] == 1 and r["ok"] == 1 and r["failed"] == []
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM16").one())
    assert b.reach_ok is True
    assert b.reach_error is None
    assert b.reach_checked_at is not None


def test_probe_reach_refused(session, monkeypatch):
    p = _prov_online(session, "ref-n")
    _register_port(session, p, com="COM4", endpoint="tcp://127.0.0.1:9211")
    monkeypatch.setattr(comres, "_tcp_probe", lambda *a, **k: (False, "refused"))
    r = comres.probe_reach(session, node_id=p["node_id"], timeout=0.5)
    assert r["checked"] == 1 and r["ok"] == 0
    assert r["failed"][0]["error"] == "refused"
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM4").one())
    assert b.reach_ok is False and b.reach_error == "refused"


def test_probe_reach_no_route(session, monkeypatch):
    p = _prov_online(session, "nr-n")
    _register_port(session, p, com="ttyUSB0",
                   endpoint="tcp://172.16.60.122:9001")
    monkeypatch.setattr(comres, "_tcp_probe", lambda *a, **k: (False, "no_route"))
    r = comres.probe_reach(session, node_id=p["node_id"], timeout=0.5)
    assert r["failed"][0]["error"] == "no_route"
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="ttyUSB0").one())
    assert b.reach_ok is False and b.reach_error == "no_route"


def test_probe_reach_timeout(session, monkeypatch):
    p = _prov_online(session, "to-n")
    _register_port(session, p, com="COM21", endpoint="tcp://127.0.0.1:9212")
    monkeypatch.setattr(comres, "_tcp_probe", lambda *a, **k: (False, "timeout"))
    r = comres.probe_reach(session, node_id=p["node_id"], timeout=0.5)
    assert r["failed"][0]["error"] == "timeout"
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM21").one())
    assert b.reach_ok is False and b.reach_error == "timeout"


def test_tcp_probe_classifies_errno(monkeypatch):
    """本地 socket mock：拒连 / 无路由 / 超时 errno 归一。"""
    import errno
    import socket

    def _raise(code):
        def _fn(*a, **k):
            raise OSError(code, errno.errorcode.get(code, "ERR"))
        return _fn

    monkeypatch.setattr(socket, "create_connection", _raise(errno.ECONNREFUSED))
    ok, err = comres._tcp_probe("tcp://127.0.0.1:9", 0.2)
    assert ok is False and err == "refused"

    monkeypatch.setattr(socket, "create_connection", _raise(errno.EHOSTUNREACH))
    ok, err = comres._tcp_probe("tcp://172.16.60.122:9001", 0.2)
    assert ok is False and err == "no_route"

    monkeypatch.setattr(
        socket, "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    ok, err = comres._tcp_probe("tcp://10.255.255.1:9", 0.2)
    assert ok is False and err == "timeout"


def test_resources_reach_fields_and_summary_unchecked(session):
    """老数据 reach_* 为空：字段透出为 null，汇总不把未测算失败。"""
    p = _prov_online(session, "sum-n")
    _register_port(session, p, com="COM27", endpoint="tcp://127.0.0.1:9213")
    view = comres.list_resources(session)
    assert "reach_summary" in view
    rows = view["allocated"] + view["bound"] + view["pool"] + view["inactive"]
    hit = [x for x in rows if x["node_id"] == p["node_id"] and x["com"] == "COM27"]
    assert len(hit) == 1
    assert hit[0]["reach_ok"] is None
    assert hit[0]["reach_error"] is None
    assert hit[0]["reach_checked_at"] is None
    assert view["reach_summary"]["fail"] == 0
    assert view["reach_summary"]["ok"] == 0
    assert view["reach_summary"]["total"] >= 1


def test_resources_reach_summary_after_probe(session, monkeypatch):
    p = _prov_online(session, "sum2-n")
    _register_port(session, p, com="COM24", endpoint="tcp://127.0.0.1:9214")
    monkeypatch.setattr(comres, "_tcp_probe", lambda *a, **k: (True, None))
    comres.probe_reach(session, node_id=p["node_id"], timeout=0.5)
    view = comres.list_resources(session)
    hit = [x for x in (view["pool"] + view["bound"] + view["allocated"]
                       + view["inactive"])
           if x["node_id"] == p["node_id"] and x["com"] == "COM24"]
    assert hit and hit[0]["reach_ok"] is True
    assert view["reach_summary"]["ok"] >= 1


def test_migrate_reach_columns_idempotent(session):
    """迁移幂等：重复 migrate 不炸，reach_* 列存在。"""
    migrate(session)
    migrate(session)
    from sqlalchemy import inspect
    cols = {c["name"] for c in inspect(session.get_bind()).get_columns("wl_port_binding")}
    assert {"reach_ok", "reach_error", "reach_checked_at"} <= cols


def test_shell_open_unreachable_returns_code(client, db, admin_cookie, monkeypatch):
    """打开串口前探测失败 → WS error code=UNREACHABLE，立刻关闭。"""
    s = db.session()
    try:
        migrate(s)
        p = _prov_online(s, "shell-nr")
        _register_port(s, p, com="ttyUSB0",
                       endpoint="tcp://172.16.60.122:9001")
        b = (s.query(WireLogPortBinding)
             .filter_by(node_id=p["node_id"], com_name="ttyUSB0").one())
        node_id, slot = p["node_id"], b.slot
    finally:
        s.close()

    monkeypatch.setattr(comres, "_tcp_probe", lambda *a, **k: (False, "no_route"))

    with client.websocket_connect(
        f"/api/wirelog/nodes/{node_id}/ports/{slot}/shell",
        cookies=admin_cookie,
    ) as ws:
        msg = ws.receive_json()
        assert msg.get("type") == "error"
        assert msg.get("code") == "UNREACHABLE"
        assert "no_route" in (msg.get("message") or "")
        assert "172.16.60.122" in (msg.get("message") or "")


def test_http_reachability_endpoint(client, db, admin_cookie, monkeypatch):
    s = db.session()
    try:
        migrate(s)
        p = _prov_online(s, "http-r")
        _register_port(s, p, com="COM4", endpoint="tcp://127.0.0.1:9220")
        node_id = p["node_id"]
    finally:
        s.close()

    monkeypatch.setattr(comres, "_tcp_probe", lambda *a, **k: (True, None))
    r = client.post(
        "/api/comres/reachability",
        json={"node_id": node_id},
        cookies=admin_cookie,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["checked"] == 1 and body["ok"] == 1
