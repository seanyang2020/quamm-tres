"""设备访问控制（v0.26）：共享范围与允许名单判定。

share_scope：
- shared    公共池，任何有效用户可申请（默认，保持历史行为）
- private   个人独享，仅注册者本人
- restricted 指定名单，注册者 + allowed_emails / allowed_agent_labels 命中者

管理员（is_admin）一律放行；注册者（registered_by）对自己设备始终放行。
名单在 Device 上以 JSON 字符串数组存储（allowed_emails / allowed_agent_labels）。
"""
from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models import Device

SHARE_SHARED = "shared"
SHARE_PRIVATE = "private"
SHARE_RESTRICTED = "restricted"
SHARE_SCOPES = (SHARE_SHARED, SHARE_PRIVATE, SHARE_RESTRICTED)


def _load_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x).strip() for x in data if str(x).strip()]


def allowed_emails(device: Device) -> list[str]:
    return _load_list(device.allowed_emails)


def allowed_agent_labels(device: Device) -> list[str]:
    return _load_list(device.allowed_agent_labels)


def can_use_device(
    session: Session,
    device: Device,
    email: str | None,
    agent_label: str | None = None,
    is_admin: bool = False,
) -> bool:
    if is_admin:
        return True
    if email and device.registered_by and email.strip().lower() == device.registered_by.strip().lower():
        return True
    scope = device.share_scope or SHARE_SHARED
    if scope == SHARE_SHARED:
        return True
    if scope == SHARE_PRIVATE:
        return False
    # restricted
    if email and email.strip().lower() in {e.lower() for e in allowed_emails(device)}:
        return True
    if agent_label and agent_label.strip() in allowed_agent_labels(device):
        return True
    return False


def serialize_acl(device: Device) -> dict:
    """共享视图（供设备序列化统一调用）。"""
    return {
        "share_scope": device.share_scope or SHARE_SHARED,
        "allowed_emails": allowed_emails(device),
        "allowed_agent_labels": allowed_agent_labels(device),
    }
