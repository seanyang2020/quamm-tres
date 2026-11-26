"""管理员调整凭据 role：POST /api/credentials/{id}/role。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.migrations import migrate
from app.models import Credential, Principal
from datetime import timedelta

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


def _cookie_for(db, *, admin: bool):
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
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}, cred.id
    finally:
        s.close()


def _user_cred_id(db) -> int:
    s = db.session()
    try:
        cred = s.query(Credential).filter_by(role="user").first()
        assert cred is not None
        return cred.id
    finally:
        s.close()


def test_admin_promotes_user_then_idempotent(client, db):
    cookies, _ = _cookie_for(db, admin=True)
    uid = _user_cred_id(db)
    r = client.post(f"/api/credentials/{uid}/role", cookies=cookies, json={"role": "admin"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["role"] == "admin"
    assert body.get("updated") is True
    assert body.get("from") == "user"
    assert body.get("to") == "admin"

    r2 = client.post(f"/api/credentials/{uid}/role", cookies=cookies, json={"role": "admin"})
    assert r2.status_code == 200, r2.text
    body2 = r2.json()
    assert body2["role"] == "admin"
    assert body2.get("idempotent") is True


def test_non_admin_forbidden(client, db):
    cookies, _ = _cookie_for(db, admin=False)
    uid = _user_cred_id(db)
    r = client.post(f"/api/credentials/{uid}/role", cookies=cookies, json={"role": "admin"})
    assert r.status_code == 403, r.text


def test_bad_role_400(client, db):
    cookies, _ = _cookie_for(db, admin=True)
    uid = _user_cred_id(db)
    r = client.post(f"/api/credentials/{uid}/role", cookies=cookies, json={"role": "super"})
    assert r.status_code == 400, r.text
    detail = r.json().get("detail") or {}
    assert isinstance(detail, dict)
    assert detail.get("code") == "BAD_ROLE"


def test_revoked_credential_409(client, db):
    cookies, _ = _cookie_for(db, admin=True)
    s = db.session()
    try:
        cred = s.query(Credential).filter_by(role="user").first()
        assert cred is not None
        cred.revoked_at = utcnow()
        s.commit()
        cid = cred.id
    finally:
        s.close()
    r = client.post(f"/api/credentials/{cid}/role", cookies=cookies, json={"role": "admin"})
    assert r.status_code == 409, r.text
    detail = r.json().get("detail") or {}
    assert isinstance(detail, dict)
    assert detail.get("code") == "CREDENTIAL_REVOKED"


def test_self_demote_forbidden_other_ok(client, db):
    cookies, self_id = _cookie_for(db, admin=True)
    r = client.post(
        f"/api/credentials/{self_id}/role",
        cookies=cookies,
        json={"role": "user"},
    )
    assert r.status_code == 403, r.text
    detail = r.json().get("detail") or {}
    assert isinstance(detail, dict)
    assert detail.get("code") == "SELF_DEMOTE_FORBIDDEN"

    # 另建一枚 admin 凭据（非当前会话），可被降级
    s = db.session()
    try:
        self_cred = s.get(Credential, self_id)
        assert self_cred is not None
        other = Credential(
            principal_id=self_cred.principal_id,
            token_hash="deadbeef" * 8,
            token_prefix="rr_other_",
            role="admin",
            note="other-admin",
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(days=90),
        )
        s.add(other)
        s.commit()
        other_id = other.id
    finally:
        s.close()

    r2 = client.post(
        f"/api/credentials/{other_id}/role",
        cookies=cookies,
        json={"role": "user"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["role"] == "user"
    assert r2.json().get("updated") is True
