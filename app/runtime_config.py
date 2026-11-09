"""运行时配置助手（v0.16）：settings 表 KV 覆盖 pydantic 默认，改完立即生效。

SMTP 仍由 mailer 单独管理（smtp.* 前缀）；此处只覆盖业务白名单键。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.config import settings as pydantic_settings
from app.models import Setting
from app.utils import utcnow

# 白名单：键 -> 类型与可选范围（int 校验 min/max；bool 无范围）
RUNTIME_SPECS: dict[str, dict[str, Any]] = {
    "patrol_interval_minutes": {"type": "int", "min": 1, "max": 1440},
    # v0.28：pending 设备专项巡检间隔（待检设备频繁探测，尽快转正）
    "pending_patrol_interval_minutes": {"type": "int", "min": 1, "max": 1440},
    # v0.28：探测后端可在线切换（mock/real）
    "probe_backend": {"type": "str", "min_length": 4, "max_length": 8, "choices": ["mock", "real"]},
    "patrol_fail_threshold": {"type": "int", "min": 1, "max": 100},
    "holder_concurrency_limit": {"type": "int", "min": 1, "max": 20},
    "allow_apply_when_suspect": {"type": "bool"},
    "lease_ttl_minutes": {"type": "int", "min": 1, "max": 1440},
    "lease_grace_minutes": {"type": "int", "min": 0, "max": 1440},
    "lease_max_minutes": {"type": "int", "min": 1, "max": 10080},
    "offer_window_seconds": {"type": "int", "min": 5, "max": 3600},
    "credential_valid_days": {"type": "int", "min": 1, "max": 3650},
    "heartbeat_required": {"type": "bool"},
    "heartbeat_interval_minutes": {"type": "int", "min": 1, "max": 1440},
    "heartbeat_miss_grace_minutes": {"type": "int", "min": 0, "max": 1440},
    # v0.24 截图保留策略（设置-截图存储在线修改）
    "screenshot_max_per_device": {"type": "int", "min": 1, "max": 1000},
    "screenshot_retention_days": {"type": "int", "min": 1, "max": 3650},
    "screenshot_total_max": {"type": "int", "min": 1, "max": 100000},
    "screenshot_cleanup_on_release": {"type": "bool"},
    # pending 设备端口敲门自动恢复
    "pending_auto_recover_enabled": {"type": "bool"},
    "pending_knock_eventcode": {"type": "str", "min_length": 3, "max_length": 6, "digits": True},
    "pending_knock_timeout": {"type": "int", "min": 5, "max": 120},
}

RUNTIME_KEYS = frozenset(RUNTIME_SPECS)


def _pydantic_default(key: str) -> Any:
    return getattr(pydantic_settings, key)


def _parse_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _parse_stored(key: str, raw: str | None) -> Any:
    spec = RUNTIME_SPECS[key]
    if raw is None or raw == "":
        return _pydantic_default(key)
    if spec["type"] == "bool":
        return _parse_bool(raw)
    if spec["type"] == "str":
        return str(raw)
    return int(raw)


def validate_runtime_value(key: str, value: Any) -> tuple[Any | None, str | None]:
    """校验并规范化单个值。成功返回 (normalized, None)，失败返回 (None, 错误说明)。"""
    if key not in RUNTIME_SPECS:
        return None, f"不支持的配置键：{key}"
    spec = RUNTIME_SPECS[key]
    if spec["type"] == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value), None
        if isinstance(value, str) and value.strip().lower() in (
            "0", "1", "true", "false", "yes", "no", "on", "off",
        ):
            return _parse_bool(value), None
        return None, f"{key} 须为布尔值"
    # str
    if spec["type"] == "str":
        if not isinstance(value, str):
            return None, f"{key} 须为字符串"
        sv = value.strip()
        lo = spec.get("min_length", 0)
        hi = spec.get("max_length", 1024)
        if len(sv) < lo or len(sv) > hi:
            return None, f"{key} 长度须在 {lo}–{hi} 之间"
        if spec.get("digits") and not sv.isdigit():
            return None, f"{key} 须为纯数字"
        choices = spec.get("choices")
        if choices and sv not in choices:
            return None, f"{key} 须为 {', '.join(choices)} 之一"
        return sv, None
    # int
    try:
        if isinstance(value, bool):
            raise ValueError("bool is not int")
        iv = int(value)
    except (TypeError, ValueError):
        return None, f"{key} 须为整数"
    lo, hi = spec["min"], spec["max"]
    if iv < lo or iv > hi:
        return None, f"{key} 须在 {lo}–{hi} 之间（当前 {iv}）"
    return iv, None


def get_runtime_value(session: Session, key: str, default: Any = None) -> Any:
    """读运行时值：DB KV 覆盖 pydantic 默认；default 显式传入时优先于 pydantic。"""
    if key not in RUNTIME_SPECS:
        if default is not None:
            return default
        raise KeyError(key)
    row = session.get(Setting, key)
    if row is not None and row.value is not None and row.value != "":
        try:
            return _parse_stored(key, row.value)
        except (TypeError, ValueError):
            pass
    if default is not None:
        return default
    return _pydantic_default(key)


def set_runtime_value(session: Session, key: str, value: Any) -> Any:
    """校验后写入 settings 表（不 commit）。返回规范化后的值；非法抛 ValueError。"""
    normalized, err = validate_runtime_value(key, value)
    if err:
        raise ValueError(err)
    # bool 必须按真实值存 true/false：否则 False 也会被写成一个 "true"，
    # 导致 allow_apply_when_suspect / heartbeat_required / screenshot_cleanup_on_release
    # 等开关「关不掉」。
    stored = (
        ("true" if normalized else "false")
        if isinstance(normalized, bool)
        else str(normalized)
    )
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=stored, updated_at=utcnow()))
    else:
        row.value = stored
        row.updated_at = utcnow()
    return normalized


def runtime_public_view(session: Session) -> dict[str, Any]:
    """GET/PUT /settings 中业务字段视图（不含 location_rule / product_types）。"""
    return {key: get_runtime_value(session, key) for key in RUNTIME_SPECS}
