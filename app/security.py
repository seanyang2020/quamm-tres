"""身份解析：token -> (principal, credential)。

每次调用只校验四条（见 09 §4）：
① token 存在且未吊销 ② 未过期 ③ 人的配额未超（在领域层校验） ④ 设备可用（在领域层校验）
这里做 ①② 与权限（is_admin），其余由领域服务负责。
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Credential, Principal
from app.utils import hash_token, utcnow


@dataclass
class AuthContext:
    principal: Principal
    credential: Credential
    email: str
    is_admin: bool


# 当前请求的已认证身份（MCP 中间件 / FastAPI 依赖写入，工具内读取）
current_auth: contextvars.ContextVar[AuthContext | None] = contextvars.ContextVar(
    "current_auth", default=None
)


class AuthError(Exception):
    """鉴权失败。message 对客户端可见，detail 只进日志。"""

    def __init__(self, message: str, status: int = 401):
        self.message = message
        self.status = status
        super().__init__(message)


def resolve_token(session: Session, token: str) -> AuthContext:
    """校验 token 并返回身份；失败抛 AuthError。"""
    if not token:
        raise AuthError("missing token", 401)

    token_hash = hash_token(token)
    credential = (
        session.query(Credential).filter(Credential.token_hash == token_hash).first()
    )
    if credential is None:
        raise AuthError("invalid token", 401)
    if credential.revoked:
        raise AuthError("credential revoked", 401)
    now = utcnow()
    if credential.expires_at is not None and credential.expires_at <= now:
        raise AuthError("credential expired", 401)

    principal = session.get(Principal, credential.principal_id)
    if principal is None or principal.status != "active":
        raise AuthError("principal inactive", 401)

    credential.last_used_at = now
    session.commit()

    # v0.11：凭据自带 role（admin/user）；同时兼容老库的 principal.is_admin 标志，
    # 保证 admin@corp.com 与历史行为不变。
    is_admin = bool(
        getattr(credential, "role", None) == "admin" or principal.is_admin
    )
    return AuthContext(
        principal=principal,
        credential=credential,
        email=principal.email,
        is_admin=is_admin,
    )


def get_current_auth() -> AuthContext | None:
    return current_auth.get()


def require_auth() -> AuthContext:
    ctx = current_auth.get()
    if ctx is None:
        raise AuthError("missing token", 401)
    return ctx


@contextmanager
def set_auth(auth: AuthContext):
    """直接调用工具/领域函数的测试用：注入身份上下文。"""
    token = current_auth.set(auth)
    try:
        yield
    finally:
        current_auth.reset(token)
