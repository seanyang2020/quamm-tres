"""通用工具：UTC 时间、ID 生成、token 生成/哈希。"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone


def utcnow() -> datetime:
    """统一 UTC 时间基准（服务端时间为唯一判定基准，见 02 R-B13）。

    存储统一 naive UTC（SQLite/PG 行为一致），展示层再转本地时区。
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id(prefix: str, nbytes: int = 6) -> str:
    return f"{prefix}{secrets.token_hex(nbytes)}"


def new_device_id() -> str:
    return new_id("dev_", 5)


def new_lease_id() -> str:
    return new_id("L-", 5)


def new_ticket_id() -> str:
    return new_id("T-", 5)


def new_form_id() -> str:
    return new_id("reg_", 4)


def new_flash_id() -> str:
    return new_id("flash_", 4)


def new_run_id() -> str:
    return new_id("run_", 4)


def new_notice_id() -> str:
    return new_id("N-", 4)


def new_token() -> str:
    """生成 rr_ 前缀 token：rr_<8 位随机>_<32 位随机>。"""
    from app.config import settings

    return f"{settings.token_prefix}{secrets.token_hex(4)}_{secrets.token_hex(16)}"


def hash_token(token: str) -> str:
    """token 只存 SHA-256 哈希。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def token_prefix(token: str) -> str:
    """token 尾号前缀，用于 Web 台账展示（不泄露全文）。"""
    if len(token) <= 12:
        return token[:4]
    return token[:12]


def mask_token(token: str) -> str:
    """日志脱敏：只保留前缀，其余打码。"""
    if not token:
        return ""
    return f"{token[:8]}...{token[-4:]}" if len(token) > 12 else "***"
