"""DELETE /api/wirelog/nodes/{id}：级联清 apply FK，禁止非 retired，不出现 500。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.migrations import migrate
from app.models import (
    Credential,
    Principal,
    WireLogAudit,
    WireLogClientApply,
    WireLogEnrollment,
    WireLogNode,
    WireLogPortBinding,
)
from app.secret_crypto import encrypt_secret
from app.utils import hash_token, utcnow


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
            p = s.get(Principal, cred.principal_id)
            p.is_admin = True
            cred.role = "admin"
            s.commit()
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}
    finally:
        s.close()


def _seed_retired_graph(session, *, node_id="wldeadnode0001", client_id="cid-del-1"):
    now = utcnow()
    plain = "rr_test_delete_token_xxxx"
    node = WireLogNode(
        node_id=node_id,
        display_name="to-delete",
        model_class="pc",
        node_token_hash=hash_token(plain),
        token_prefix=plain[:8],
        token_cipher=encrypt_secret(plain),
        status="retired",
        created_at=now,
        provisioned_at=now,
    )
    session.add(node)
    session.flush()
    session.add(WireLogPortBinding(node_id=node_id, slot=1, baud=115200))
    session.add(WireLogAudit(
        node_id=node_id, action="retire", actor="tester",
        result="ok", created_at=now,
    ))
    session.add(WireLogEnrollment(
        node_id=node_id, operator="tester", result="ok", created_at=now,
    ))
    apply = WireLogClientApply(
        client_id=client_id,
        display_name="desk-del",
        status="approved",
        request_count=1,
        first_seen_at=now,
        last_seen_at=now,
        approved_at=now,
        approved_by="system",
        issued_node_id=node_id,
        token_cipher=encrypt_secret(plain),
        token_prefix=plain[:8],
    )
    session.add(apply)
    session.commit()
    return node_id, client_id, apply.id


def test_delete_retired_cascades_apply_and_refs(client, db, admin_cookie):
    s = db.session()
    try:
        node_id, client_id, apply_id = _seed_retired_graph(s)
    finally:
        s.close()

    r = client.delete(f"/api/wirelog/nodes/{node_id}", cookies=admin_cookie)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["deleted"] is True
    assert body["node_id"] == node_id
    cascade = body["cascade"]
    assert cascade["bindings"] >= 1
    assert cascade["audits"] >= 1
    assert cascade["enrollments"] >= 1
    assert cascade["applies"] >= 1

    s = db.session()
    try:
        assert s.get(WireLogNode, node_id) is None
        assert s.query(WireLogPortBinding).filter_by(node_id=node_id).count() == 0
        assert s.query(WireLogAudit).filter_by(node_id=node_id).count() == 0
        assert s.query(WireLogEnrollment).filter_by(node_id=node_id).count() == 0
        row = s.get(WireLogClientApply, apply_id)
        assert row is not None
        assert row.client_id == client_id
        assert row.issued_node_id is None
        assert row.token_cipher is None
        assert row.token_prefix  # 追溯字段保留
        assert row.first_seen_at is not None
    finally:
        s.close()


def test_delete_non_retired_rejected(client, db, admin_cookie):
    s = db.session()
    try:
        now = utcnow()
        node_id = "wlonlinenode001"
        s.add(WireLogNode(
            node_id=node_id,
            display_name="alive",
            node_token_hash=hash_token("rr_alive"),
            status="online",
            created_at=now,
        ))
        s.commit()
    finally:
        s.close()

    r = client.delete(f"/api/wirelog/nodes/{node_id}", cookies=admin_cookie)
    assert r.status_code in (400, 409), r.text
    assert r.status_code != 500
    detail = r.json().get("detail") or {}
    msg = detail if isinstance(detail, str) else (
        detail.get("message") or detail.get("code") or str(detail))
    assert "退役" in str(msg) or "WL_NOT_RETIRED" in str(detail)
    assert "IntegrityError" not in r.text


def test_delete_requires_admin(client, db):
    s = db.session()
    try:
        node_id, _, _ = _seed_retired_graph(s, node_id="wlneedadmin001",
                                            client_id="cid-admin-gate")
        user = s.query(Principal).filter_by(is_admin=False).first()
        assert user is not None
        cred = s.query(Credential).filter_by(principal_id=user.id).first()
        user_cookie = {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}
    finally:
        s.close()

    r = client.delete(f"/api/wirelog/nodes/{node_id}", cookies=user_cookie)
    assert r.status_code == 403
    assert r.status_code != 500
    assert "管理员" in str(r.json().get("detail"))


def test_reapply_after_node_delete_reissues(client, db, admin_cookie):
    """硬删后同 client_id 再申请应重新签发，不返回旧已签发死 token。"""
    s = db.session()
    try:
        node_id, client_id, apply_id = _seed_retired_graph(
            s, node_id="wlreissue0001", client_id="cid-reissue-1")
    finally:
        s.close()

    assert client.delete(
        f"/api/wirelog/nodes/{node_id}", cookies=admin_cookie
    ).status_code == 200

    r = client.post("/api/comres/apply", json={
        "client_id": client_id, "display_name": "desk-re",
    })
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["status"] == "approved"
    assert j["node_id"] and j["node_id"] != node_id
    assert j["node_token"]

    s = db.session()
    try:
        row = s.get(WireLogClientApply, apply_id)
        assert row.issued_node_id == j["node_id"]
        assert row.token_cipher
        assert s.get(WireLogNode, j["node_id"]) is not None
    finally:
        s.close()
