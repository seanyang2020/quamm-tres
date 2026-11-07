"""凭据：过期 / 吊销 校验。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import Credential, Principal
from app.security import AuthError, resolve_token
from app.utils import utcnow


def test_expired_credential_rejected(db):
    token = db.token("user05@corp.com")
    s = db.session()
    p = s.query(Principal).filter(Principal.email == "user05@corp.com").first()
    c = s.query(Credential).filter(Credential.principal_id == p.id).first()
    c.expires_at = utcnow() - timedelta(days=1)
    s.commit()
    with pytest.raises(AuthError) as ei:
        resolve_token(s, token)
    assert "expired" in str(ei.value.message)
    s.close()


def test_revoked_credential_rejected(db):
    token = db.token("user06@corp.com")
    s = db.session()
    p = s.query(Principal).filter(Principal.email == "user06@corp.com").first()
    c = s.query(Credential).filter(Credential.principal_id == p.id).first()
    c.revoked_at = utcnow()
    s.commit()
    with pytest.raises(AuthError) as ei:
        resolve_token(s, token)
    assert "revoked" in str(ei.value.message)
    s.close()


def test_valid_credential_resolves(db):
    token = db.token("user07@corp.com")
    s = db.session()
    ctx = resolve_token(s, token)
    assert ctx.email == "user07@corp.com"
    assert ctx.is_admin is False
    s.close()


def test_invalid_token_rejected(db):
    s = db.session()
    with pytest.raises(AuthError):
        resolve_token(s, "rr_deadbeef_00000000000000000000000000000000")
    s.close()
