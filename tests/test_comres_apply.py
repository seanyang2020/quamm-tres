"""COMRes 客户端自助申请：自动批准、人工审批、离线幂等 token、撤销下架。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.domain import comres, comres_apply, wirelog
from app.migrations import migrate
from app.models import Credential, WireLogClientApply, WireLogNode, WireLogPortBinding
from app.utils import utcnow


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
        cred = s.query(Credential).first()
        assert cred is not None
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}
    finally:
        s.close()


def test_auto_approve_returns_token_and_idempotent(client, db):
    body = {
        "client_id": "11111111-2222-3333-4444-555555555555",
        "display_name": "desk-A",
        "model_class": "pc",
        "client_version": "1.0.0",
    }
    r1 = client.post("/api/comres/apply", json=body)
    assert r1.status_code == 200, r1.text
    j1 = r1.json()
    assert j1["status"] == "approved"
    assert j1["node_id"] and j1["node_token"]
    assert j1["request_id"]
    token1 = j1["node_token"]

    r2 = client.post("/api/comres/apply", json=body)
    assert r2.status_code == 200
    j2 = r2.json()
    assert j2["status"] == "approved"
    assert j2["node_id"] == j1["node_id"]
    assert j2["node_token"] == token1
    assert j2["node_token"].encode("utf-8") == token1.encode("utf-8")

    # GET 轮询同样幂等
    r3 = client.get(f"/api/comres/apply/{body['client_id']}")
    assert r3.status_code == 200
    assert r3.json()["node_token"] == token1

    s = db.session()
    try:
        row = s.query(WireLogClientApply).filter_by(client_id=body["client_id"]).one()
        assert row.token_cipher
        assert "rr_" not in (row.token_cipher or "")  # 密文非明文
        assert row.request_count >= 2
    finally:
        s.close()


def test_manual_approve_then_poll_same_token(client, db, admin_cookie):
    s = db.session()
    try:
        comres_apply.set_auto_approve(s, False)
    finally:
        s.close()

    cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    r = client.post("/api/comres/apply", json={
        "client_id": cid, "display_name": "desk-B",
    })
    assert r.status_code == 200
    assert r.json()["status"] == "pending"
    rid = r.json()["request_id"]

    # 未登录管理面 401
    assert client.get("/api/comres/applications").status_code == 401
    assert client.post(f"/api/comres/applications/{rid}/approve", json={}).status_code == 401

    ar = client.post(
        f"/api/comres/applications/{rid}/approve",
        json={"note": "ok"},
        cookies=admin_cookie,
    )
    assert ar.status_code == 200, ar.text
    token = ar.json()["node_token"]
    assert ar.json()["status"] == "approved"

    gr = client.get(f"/api/comres/apply/{cid}")
    assert gr.status_code == 200
    assert gr.json()["node_token"] == token
    assert gr.json()["node_id"] == ar.json()["node_id"]


def test_reject_then_apply_returns_rejected(client, db, admin_cookie):
    s = db.session()
    try:
        comres_apply.set_auto_approve(s, False)
    finally:
        s.close()

    cid = "ffffffff-0000-1111-2222-333333333333"
    r = client.post("/api/comres/apply", json={"client_id": cid, "display_name": "x"})
    rid = r.json()["request_id"]

    rr = client.post(
        f"/api/comres/applications/{rid}/reject",
        json={"reason": "非白名单机房"},
        cookies=admin_cookie,
    )
    assert rr.status_code == 200
    assert rr.json()["status"] == "rejected"

    r2 = client.post("/api/comres/apply", json={"client_id": cid, "display_name": "x"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "rejected"
    assert "非白名单" in (r2.json().get("reason") or "")


def test_different_client_ids_independent_nodes(client, db):
    a = client.post("/api/comres/apply", json={
        "client_id": "c1-aaaa-bbbb-cccc-dddddddddddd", "display_name": "A",
    }).json()
    b = client.post("/api/comres/apply", json={
        "client_id": "c2-aaaa-bbbb-cccc-dddddddddddd", "display_name": "B",
    }).json()
    assert a["node_id"] != b["node_id"]
    assert a["node_token"] != b["node_token"]


def test_revoke_retires_node_and_removes_pool(client, db, admin_cookie):
    body = {
        "client_id": "rev-aaaa-bbbb-cccc-dddddddddddd",
        "display_name": "desk-rev",
    }
    ap = client.post("/api/comres/apply", json=body).json()
    token, node_id, rid = ap["node_token"], ap["node_id"], ap["request_id"]

    # 注册端口进入资源池
    reg = client.post(
        "/api/comres/register",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "node_id": node_id,
            "local_ip": "10.9.9.9",
            "ports": [{
                "slot": 1, "com": "COM9",
                "endpoint": "tcp://10.9.9.9:9300",
                "health_state": "online",
            }],
        },
    )
    assert reg.status_code == 200, reg.text

    pool_before = client.get("/api/comres/resources", cookies=admin_cookie).json()["pool"]
    assert any(p["node_id"] == node_id for p in pool_before)

    rv = client.post(
        f"/api/comres/applications/{rid}/revoke",
        cookies=admin_cookie,
    )
    assert rv.status_code == 200, rv.text
    assert rv.json()["status"] == "revoked"
    assert rv.json()["bindings_removed"] >= 1

    s = db.session()
    try:
        node = s.get(WireLogNode, node_id)
        assert node is not None
        assert node.status == "retired"
        bindings = s.query(WireLogPortBinding).filter_by(node_id=node_id).all()
        assert bindings == []
        row = s.get(WireLogClientApply, rid)
        assert row.status == "revoked"
    finally:
        s.close()

    pool_after = client.get("/api/comres/resources", cookies=admin_cookie).json()["pool"]
    assert not any(p["node_id"] == node_id for p in pool_after)


def test_apply_settings_roundtrip(client, db, admin_cookie):
    assert client.get("/api/comres/apply-settings").status_code == 401
    r = client.get("/api/comres/apply-settings", cookies=admin_cookie)
    assert r.status_code == 200
    assert r.json()["auto_approve"] is True

    r2 = client.put(
        "/api/comres/apply-settings",
        json={"auto_approve": False},
        cookies=admin_cookie,
    )
    assert r2.status_code == 200
    assert r2.json()["auto_approve"] is False
    assert client.get("/api/comres/apply-settings", cookies=admin_cookie).json()["auto_approve"] is False

    client.put(
        "/api/comres/apply-settings",
        json={"auto_approve": True},
        cookies=admin_cookie,
    )


def test_pending_ports_excluded_from_pool(db):
    """待审节点端口不入可分配池。"""
    s = db.session()
    try:
        migrate(s)
        comres_apply.set_auto_approve(s, False)
        # 直接建 pending 申请 + 节点（模拟半手工）
        now = utcnow()
        n = wirelog.provision_host(s, display_name="pending-node", operator="t")
        s.add(WireLogClientApply(
            client_id="pend-client-0001",
            display_name="pending-node",
            status="pending",
            request_count=1,
            first_seen_at=now,
            last_seen_at=now,
            issued_node_id=n["node_id"],
        ))
        s.commit()
        comres.register(s, n["node_token"], {
            "node_id": n["node_id"], "local_ip": "10.1.1.1",
            "ports": [{"slot": 1, "com": "COM1",
                       "endpoint": "tcp://10.1.1.1:9100",
                       "health_state": "online"}],
        })
        view = comres.list_resources(s)
        assert not any(p["node_id"] == n["node_id"] for p in view["pool"])
    finally:
        s.close()
