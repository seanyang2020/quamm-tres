"""Web 登录会话（v0.11）：HttpOnly 签名 cookie + 角色依赖。

一套 rr_ token 两用：
- MCP 端仍走 Authorization: Bearer 头，完全不读 cookie（见 mcp/middleware.py）。
- Web 端用 /api/auth/login 以 rr_ token 换一个短期会话 cookie，后续请求带 cookie 即可。

cookie 里只存 credential_id，服务端每次请求复核凭据未吊销 / 未过期（吊销即时生效），
签名用 HMAC-SHA256，避免伪造。read-only 展示接口用 optional_identity（可匿名），
管理 / 写操作用 require_login（未登录 401）与 require_admin（user 403）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_session
from app.models import Contact, Credential, Principal
from app.utils import utcnow

SESSION_COOKIE = "quamm_session"
_ROLE_ADMIN = "admin"
_ROLE_USER = "user"


@dataclass
class WebIdentity:
    email: str
    name: str | None
    role: str                 # admin | user
    principal_id: int
    credential_id: int


def _secret() -> bytes:
    return settings.session_secret.encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def make_session(credential_id: int) -> str:
    payload = {
        "cid": credential_id,
        "exp": int(time.time()) + settings.web_session_hours * 3600,
    }
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


def read_session(token: str | None) -> dict | None:
    if not token:
        return None
    body, dot, sig = token.partition(".")
    if not dot:
        return None
    expected = _b64e(hmac.new(_secret(), body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, expected):
        return None
    try:
        data = json.loads(_b64d(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < int(time.time()):
        return None
    return data


def contact_name(session: Session, email: str) -> str | None:
    c = session.get(Contact, email)
    return c.name if c and c.name else None


def optional_identity(
    request: Request, session: Session = Depends(get_session)
) -> WebIdentity | None:
    """解析 cookie；无 / 无效 / 凭据已吊销 -> None（不抛错，供只读展示接口使用）。"""
    data = read_session(request.cookies.get(SESSION_COOKIE))
    if data is None:
        return None
    cred = session.get(Credential, data.get("cid"))
    if cred is None or cred.revoked:
        return None
    if cred.expires_at is not None and cred.expires_at <= utcnow():
        return None
    principal = session.get(Principal, cred.principal_id)
    if principal is None or principal.status != "active":
        return None
    role = _ROLE_ADMIN if (cred.role == _ROLE_ADMIN or principal.is_admin) else _ROLE_USER
    name = principal.name or contact_name(session, principal.email)
    return WebIdentity(
        email=principal.email,
        name=name,
        role=role,
        principal_id=principal.id,
        credential_id=cred.id,
    )


def require_login(identity: WebIdentity | None = Depends(optional_identity)) -> WebIdentity:
    if identity is None:
        raise HTTPException(status_code=401, detail="未登录")
    return identity


def require_admin(identity: WebIdentity = Depends(require_login)) -> WebIdentity:
    if identity.role != _ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="仅管理员可用")
    return identity
