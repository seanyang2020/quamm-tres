"""MCP streamable HTTP 最小客户端 + 演示用 token 供应（scripts/ 内部共用）。

只通过 HTTP JSON-RPC 调用服务端工具，不导入、不直接调用任何领域函数。
token 供应复用 app.seed 的官方签发逻辑（rotate），明文缓存到
scripts/.demo_tokens.json（0600，已 gitignore），避免每次演示都吊销旧凭据。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
TOKEN_CACHE = Path(__file__).resolve().parent / ".demo_tokens.json"

MCP_URL = os.environ.get("RES_DEMO_MCP_URL", "http://127.0.0.1:8080/mcp/")
API_BASE = os.environ.get("RES_DEMO_API_BASE", "http://127.0.0.1:8080")


class McpError(RuntimeError):
    """JSON-RPC 或工具返回 error 时抛出。"""

    def __init__(self, message: str, code: str | None = None):
        self.code = code
        super().__init__(message)


def mask_token(token: str) -> str:
    if not token:
        return "<none>"
    return f"{token[:8]}...{token[-4:]}" if len(token) > 12 else "***"


class McpClient:
    """一个 token 对应一个客户端（复用连接）。"""

    def __init__(self, token: str, url: str = MCP_URL, timeout: float = 15.0):
        self.token = token
        self.url = url
        self._id = 0
        self._http = httpx.Client(
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def call(self, name: str, arguments: dict | None = None):
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        resp = self._http.post(self.url, json=payload)
        if resp.status_code == 401:
            raise McpError("AUTH: token 无效或已吊销", code="AUTH")
        resp.raise_for_status()
        body = _parse_body(resp)
        if "error" in body:
            err = body["error"]
            raise McpError(err.get("message", str(err)), code="RPC")
        result = body["result"]
        text = result["content"][0]["text"]
        data = json.loads(text)
        if isinstance(data, dict) and "error" in data:
            raise McpError(data.get("message", data["error"]), code=data["error"])
        return data


def _parse_body(resp: httpx.Response) -> dict:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[len("data:"):].strip())
        raise McpError("SSE 响应中没有 data 行")
    return resp.json()


# ---------- token 供应 ----------

def _read_cache() -> dict[str, str] | None:
    if not TOKEN_CACHE.exists():
        return None
    try:
        data = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and len(data) == 20:
        return data
    return None


def _write_cache(tokens: dict[str, str]) -> None:
    TOKEN_CACHE.write_text(json.dumps(tokens, ensure_ascii=False, indent=2), encoding="utf-8")
    TOKEN_CACHE.chmod(0o600)


def refresh_tokens(count: int = 20) -> dict[str, str]:
    """吊销旧凭据并重新签发 20 条（token 只存哈希，明文无法反查）。"""
    from app.db import SessionLocal, init_db
    from app.seed import seed_product_types, seed_tokens

    init_db()
    session = SessionLocal()
    try:
        seed_product_types(session)
        pairs = seed_tokens(session, count=count, rotate=True)
    finally:
        session.close()
    tokens = dict(pairs)
    if len(tokens) != count:
        raise McpError(f"token 签发数量异常：期望 {count}，实际 {len(tokens)}")
    _write_cache(tokens)
    return tokens


def load_tokens(count: int = 20) -> dict[str, str]:
    """优先用本地缓存，缺失时签发一批新的。"""
    cached = _read_cache()
    if cached is not None:
        return cached
    return refresh_tokens(count)
