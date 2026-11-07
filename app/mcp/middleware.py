"""MCP 鉴权中间件：从请求中提取 token、解析身份、写入上下文、日志脱敏。

优先级（见 01 §1.2 / 任务）：Authorization: Bearer rr_... 优先，兼容 ?t=rr_...。
两种方式日志都脱敏。
"""
from __future__ import annotations

import logging
import urllib.parse

from app.db import SessionLocal
from app.security import AuthError, current_auth, resolve_token
from app.utils import mask_token

logger = logging.getLogger(__name__)


def _extract_token(scope: dict) -> str | None:
    headers = dict(scope.get("headers", []))
    auth_header = headers.get(b"authorization")
    if auth_header:
        value = auth_header.decode("latin-1", errors="ignore")
        scheme, _, token = value.partition(" ")
        if scheme.strip().lower() == "bearer" and token.strip():
            return token.strip()

    qs = scope.get("query_string", b"")
    if qs:
        params = urllib.parse.parse_qs(qs.decode("latin-1", errors="ignore"))
        t = params.get("t")
        if t and t[0]:
            return t[0]
    return None


class McpAuthMiddleware:
    """纯 ASGI 中间件，包裹 MCP 端点。"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = _extract_token(scope)
        logger.debug("MCP auth token=%s", mask_token(token) if token else "<none>")

        if not token:
            await self._reject(send, 401, "missing token")
            return

        try:
            session = SessionLocal()
            try:
                auth = resolve_token(session, token)
            finally:
                session.close()
        except AuthError as e:
            await self._reject(send, e.status, e.message)
            return

        token_ctx = current_auth.set(auth)
        try:
            await self.app(scope, receive, send)
        finally:
            current_auth.reset(token_ctx)

    async def _reject(self, send, status: int, message: str):
        body = f'{{"jsonrpc":"2.0","error":{{"code":{status},"message":"{message}"}}}}'.encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
