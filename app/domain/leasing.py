"""租约引擎（见 02 B 域、03 §4、05 §7 实现约定）。

- TTL + 主动续约 + 宽限期，不做高频心跳
- 释放 / 超期回收与"唤醒队首 offer"在同一事务内完成
- 活跃租约每台设备至多一条，由数据库部分唯一索引兜底防并发超发
"""
from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.constants import (
    AGENT_CONTRACT_VERSION,
    DeviceStatus,
    HealthLevel,
    LeaseState,
    QueueState,
)
from app.config import settings
from app.domain import audit, flash
from app.domain import devicelock
from app.domain.catalog import DomainError
from app.domain.queueing import cancel, enqueue, find_head, position
from app.domain.notices import create_notice
from app.models import Device, Lease, Principal, ProductType, QueueEntry
from app.runtime_config import get_runtime_value
from app.security import AuthContext
from app.utils import new_lease_id, utcnow

logger = logging.getLogger("quamm-tres.leasing")


def _clean_agent_info(value) -> str | None:
    """调用方 Agent 自报身份（如 WorkBuddy/豆包，可带版本），截断到 128 字符。"""
    if value is None:
        return None
    text = str(value).strip()
    return text[:128] or None


def _ttl_minutes(session: Session, expected_minutes: int | None) -> int:
    lease_max = int(get_runtime_value(session, "lease_max_minutes"))
    lease_ttl = int(get_runtime_value(session, "lease_ttl_minutes"))
    if expected_minutes:
        return int(min(expected_minutes * 1.5, lease_max))
    return lease_ttl


def count_active_leases(session: Session, principal_id: int) -> int:
    return (
        session.query(func.count(Lease.lease_id))
        .filter(Lease.principal_id == principal_id, Lease.state.in_(LeaseState.ACTIVE))
        .scalar()
        or 0
    )


def _lease_dict(lease: Lease, device: Device | None = None) -> dict:
    d = device or (lease.device if lease.device else None)
    return {
        "lease_id": lease.lease_id,
        "device_id": lease.device_id,
        "state": lease.state,
        "adb_address": lease.adb_address,
        "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
        "ttl_minutes": lease.ttl_minutes,
        "purpose": lease.purpose,
        "granted_at": lease.granted_at.isoformat() if lease.granted_at else None,
        "agent_label": lease.credential.agent_label if lease.credential else None,
        "display_name": d.display_name if d else None,
    }


def _classify_target(session: Session, target: str) -> tuple[str, str]:
    """target 填 device_id 或机型 type_code（旧用法）。"""
    dev = session.get(Device, target)
    if dev is not None:
        return "device", target
    pt = session.get(ProductType, target)
    if pt is not None:
        return "product", target
    raise DomainError("UNKNOWN_TARGET", f"目标 {target} 既不是 device_id 也不是机型")


def _resolve_target(
    session: Session,
    target: str | None,
    product_type: str | None,
    device_id: str | None,
) -> tuple[str, str]:
    """v0.12：res_apply 两种方式二选一。

    - product_type：按机型池申请（product 队列，任意同机型 available 设备空闲即分配）
    - device_id：指定具体设备（兼容旧用法）
    - target：旧单参数用法（device_id 或 type_code），保留以不破坏历史客户端/验收
    """
    if product_type and device_id:
        raise DomainError("AMBIGUOUS_TARGET", "product_type 与 device_id 只能二选一")
    if product_type:
        pt = session.get(ProductType, product_type)
        if pt is None:
            raise DomainError("UNKNOWN_TARGET", f"机型 {product_type} 不存在于机型字典")
        return "product", product_type
    if device_id:
        dev = session.get(Device, device_id)
        if dev is None:
            raise DomainError("UNKNOWN_TARGET", f"设备 {device_id} 不存在")
        return "device", device_id
    if target:
        return _classify_target(session, target)
    raise DomainError(
        "MISSING_TARGET", "请提供 product_type（按机型）或 device_id（指定设备）"
    )


def _find_available(
    session: Session,
    target_type: str,
    target_code: str,
    product: ProductType | None = None,
) -> tuple[Device | None, bool, dict | None]:
    """按可用池口径找设备，并做 v0.14 固件版本门禁。

    返回 (device, had_candidates, verdict)：
    - device 为 None 且 had_candidates=False：本来就没有可申请设备（走排队）
    - device 为 None 且 had_candidates=True：有设备但版本全不满足（NO_COMPATIBLE_DEVICE）
    - verdict 为命中的版本判定（UNKNOWN_VERSION / PATCH_BEHIND / OK）
    """
    # 与 res_list_devices / dashboard 同一口径：available 且可申请。
    # suspect（L1/L2）默认不可申请，只有 allow_apply_when_suspect=True 才放行；
    # L0 无论开关如何都不进候选。
    claimable_levels = [HealthLevel.L3]
    if get_runtime_value(session, "allow_apply_when_suspect"):
        claimable_levels += [HealthLevel.L1, HealthLevel.L2]
    q = session.query(Device).filter(
        Device.status == DeviceStatus.AVAILABLE,
        Device.health_level.in_(claimable_levels),
    )
    if target_type == "device":
        q = q.filter(Device.device_id == target_code)
    else:
        q = q.filter(Device.model_class == target_code)
    candidates = q.order_by(Device.created_at.asc()).all()
    if not candidates:
        return None, False, None

    last_verdict: dict | None = None
    for device in candidates:
        gate_product = product
        if gate_product is None and device.model_class:
            gate_product = session.get(ProductType, device.model_class)
        if gate_product is None:
            return device, True, None
        verdict = flash.evaluate_version(
            device.firmware_version,
            gate_product.required_major,
            gate_product.required_minor,
            gate_product.required_patch,
        )
        last_verdict = verdict
        # X/Y 不满足直接跳过这台，继续找下一台同型设备
        if verdict["state"] in ("INCOMPATIBLE", "UPGRADE_REQUIRED"):
            continue
        return device, True, verdict
    return None, True, last_verdict


def _offer_to(session: Session, device: Device, entry: QueueEntry) -> Lease:
    now = utcnow()
    ttl = int(get_runtime_value(session, "lease_ttl_minutes"))
    offer_window = int(get_runtime_value(session, "offer_window_seconds"))
    lease = Lease(
        lease_id=new_lease_id(),
        device_id=device.device_id,
        principal_id=entry.principal_id,
        credential_id=entry.credential_id,
        purpose=entry.purpose,
        ttl_minutes=ttl,
        state=LeaseState.RESERVED,
        expires_at=now + timedelta(seconds=offer_window),
        adb_address=device.adb_address,
        created_at=now,
        last_heartbeat_at=now,
    )
    session.add(lease)
    session.flush()
    entry.state = QueueState.OFFERED
    entry.offered_at = now
    entry.offer_expires_at = now + timedelta(seconds=offer_window)
    entry.lease_id = lease.lease_id
    device.status = DeviceStatus.OCCUPIED
    audit.record(
        session, actor=entry.principal.email if entry.principal else "system",
        action="offer", device_id=device.device_id, lease_id=lease.lease_id,
        detail={"ticket_id": entry.entry_id},
    )
    return lease


def _wake_queue(session: Session, device: Device) -> None:
    """设备空闲后，只向队首发 offer；无人排队则转 available。"""
    head = find_head(session, "device", device.device_id)
    if head is None and device.model_class:
        head = find_head(session, "product", device.model_class)
    if head is not None:
        _offer_to(session, device, head)
    else:
        device.status = DeviceStatus.AVAILABLE


def _reclaim(session: Session, lease: Lease, new_state: str, end_reason: str, actor: str) -> None:
    lease.state = new_state
    lease.released_at = utcnow()
    lease.end_reason = end_reason
    device = session.get(Device, lease.device_id)
    audit.record(
        session, actor=actor, action=end_reason,
        device_id=lease.device_id, lease_id=lease.lease_id,
    )
    if device is not None:
        if device.status == DeviceStatus.PENDING_REMOVAL:
            device.status = DeviceStatus.RETIRED
            device.retired_at = utcnow()
        else:
            _wake_queue(session, device)
    # 物理闸门：服务端兜底删设备锁并断开 ADB（best-effort，不依赖 Agent 自觉）
    addr = lease.adb_address or (device.adb_address if device is not None else None)
    if addr and devicelock.physical_gate_enabled:
        try:
            devicelock.remove_lock(addr, expected_lease_id=lease.lease_id)
            devicelock.disconnect(addr)
        except Exception:  # noqa: BLE001
            logger.warning("device lock cleanup failed for %s (%s)", addr, lease.lease_id)


def apply(
    session: Session,
    auth: AuthContext,
    target: str | None = None,
    *,
    product_type: str | None = None,
    device_id: str | None = None,
    purpose: str | None = None,
    expected_minutes: int | None = None,
    priority: int = 0,
    agent_info: str | None = None,
) -> dict:
    principal = auth.principal
    credential_id = auth.credential.id

    # v0.18 硬门禁：申请必须自报 Agent 身份（如 WorkBuddy/豆包，可带版本），缺失直接拒绝。
    agent_info = _clean_agent_info(agent_info)
    if not agent_info:
        raise DomainError(
            "AGENT_INFO_REQUIRED",
            "申请设备必须带 agent_info（调用方 Agent 名称/版本），请升级接入客户端后重试",
            {"required_field": "agent_info", "agent_contract_version": AGENT_CONTRACT_VERSION},
        )
    # 回写到凭据，看板/凭据页可识别该 token 用的是哪个 Agent。
    auth.credential.agent_label = agent_info

    active = count_active_leases(session, principal.id)
    if active >= principal.quota:
        raise DomainError(
            "QUOTA_EXCEEDED",
            f"已占用 {active}/{principal.quota} 台，达到配额上限",
            {"active": active, "quota": principal.quota},
        )

    target_type, target_code = _resolve_target(session, target, product_type, device_id)

    # 幂等兜底：同一人对同一目标已有活跃租约则直接返回（02 R-B10）
    if target_type == "device":
        existing = (
            session.query(Lease)
            .filter(
                Lease.principal_id == principal.id,
                Lease.device_id == target_code,
                Lease.state.in_(LeaseState.ACTIVE),
            )
            .first()
        )
        if existing is not None:
            return {"state": "GRANTED", "lease": _lease_dict(existing)}

    gate_product = (
        session.get(ProductType, target_code) if target_type == "product" else None
    )
    device, had_candidates, verdict = _find_available(
        session, target_type, target_code, gate_product
    )
    if device is None and had_candidates:
        # 有可用设备但固件版本全部不满足：不排队，明确报无兼容设备
        reason = (verdict or {}).get("reason") or "固件版本不满足要求"
        raise DomainError(
            "NO_COMPATIBLE_DEVICE",
            f"{target_code} 当前可用设备固件版本均不满足要求：{reason}",
            {
                "target_type": target_type,
                "target_code": target_code,
                "state": (verdict or {}).get("state"),
                "recommendation": (verdict or {}).get("recommendation"),
            },
        )
    if device is not None:
        ttl = _ttl_minutes(session, expected_minutes)
        now = utcnow()
        lease = Lease(
            lease_id=new_lease_id(),
            device_id=device.device_id,
            principal_id=principal.id,
            credential_id=credential_id,
            purpose=purpose,
            ttl_minutes=ttl,
            state=LeaseState.GRANTED,
            granted_at=now,
            expires_at=now + timedelta(minutes=ttl),
            adb_address=device.adb_address,
            created_at=now,
            last_heartbeat_at=now,
        )
        device.status = DeviceStatus.OCCUPIED
        device.register_agent = agent_info
        session.add(lease)
        audit.record(
            session, actor=principal.email, action="apply",
            device_id=device.device_id, lease_id=lease.lease_id, detail={"purpose": purpose},
        )
        try:
            session.commit()
            result = {
                "state": "GRANTED",
                "lease": _lease_dict(lease, device),
                "renew_hint": "长任务请到期前调 res_renew 续约",
                "agent_contract_version": AGENT_CONTRACT_VERSION,
                "heartbeat": {
                    "tool": "res_heartbeat",
                    "how_to": "每 interval_minutes 内调用一次 res_heartbeat(lease_id, agent_info)，超时未心跳将被强制回收",
                    "required": bool(get_runtime_value(session, "heartbeat_required")),
                    "interval_minutes": int(get_runtime_value(session, "heartbeat_interval_minutes")),
                    "miss_grace_minutes": int(get_runtime_value(session, "heartbeat_miss_grace_minutes")),
                    "next_heartbeat_by": (
                        now + timedelta(
                            minutes=int(get_runtime_value(session, "heartbeat_interval_minutes"))
                        )
                    ).isoformat(),
                },
                "device_lock_path": settings.device_lock_path,
            }
            # v0.14：版本门禁只拦 X/Y；未知版本放行但警告，patch 落后仅提示
            state = (verdict or {}).get("state")
            if state == "UNKNOWN_VERSION":
                result["firmware_warning"] = "UNKNOWN_VERSION"
                result["firmware_hint"] = (verdict or {}).get("recommendation", "")
            elif state == "PATCH_BEHIND":
                result["firmware_hint"] = (verdict or {}).get("recommendation", "")
            return result
        except IntegrityError:
            # 并发下这台设备已被别人抢先（数据库唯一索引兜底）
            session.rollback()
            # 落回排队分支
            device = None

    # 无空闲 -> 排队
    entry = enqueue(session, principal, credential_id, target_type, target_code, purpose, priority)
    pos = position(session, entry)
    session.commit()
    return {
        "state": "QUEUED",
        "ticket_id": entry.entry_id,
        "position": pos,
        "target_type": target_type,
        "target_code": target_code,
        "target": target_code,
        "poll_hint": "调 res_ticket(ticket_id) 查询位次；资源释放时会向你发 offer（60 秒确认窗口）",
    }


def renew(session: Session, auth: AuthContext, lease_id: str, extra_minutes: int = 30) -> dict:
    lease = session.get(Lease, lease_id)
    if lease is None:
        raise DomainError("NOT_FOUND", f"lease {lease_id} not found")
    if lease.principal_id != auth.principal.id and not auth.is_admin:
        raise DomainError("FORBIDDEN", "只能续约自己的租约")
    if lease.state not in LeaseState.ACTIVE:
        raise DomainError("INVALID_STATE", f"租约状态 {lease.state} 不可续约")

    now = utcnow()
    base = max(lease.expires_at, now) if lease.expires_at else now
    lease_max = int(get_runtime_value(session, "lease_max_minutes"))
    new_expiry = min(base + timedelta(minutes=extra_minutes), now + timedelta(minutes=lease_max))
    lease.expires_at = new_expiry
    lease.state = LeaseState.GRANTED  # 宽限期内续约恢复 ACTIVE
    audit.record(session, actor=auth.email, action="renew", device_id=lease.device_id, lease_id=lease_id)
    session.commit()
    return {"lease_id": lease_id, "state": lease.state, "expires_at": lease.expires_at.isoformat()}


def heartbeat(
    session: Session, auth: AuthContext, lease_id: str,
    agent_info: str | None = None,
) -> dict:
    """Agent 显式确认仍在使用（v0.18）。心跳即续约：刷新 last_heartbeat_at 并顺延 TTL，
    受 lease_max 绝对上限约束；宽限期内心跳恢复 GRANTED。
    """
    lease = session.get(Lease, lease_id)
    if lease is None:
        raise DomainError("NOT_FOUND", f"lease {lease_id} not found")
    if lease.principal_id != auth.principal.id and not auth.is_admin:
        raise DomainError("FORBIDDEN", "只能对自己的租约发心跳")
    if lease.state not in LeaseState.ACTIVE:
        raise DomainError("INVALID_STATE", f"租约状态 {lease.state} 不可发心跳")

    now = utcnow()
    lease.last_heartbeat_at = now
    ttl = int(get_runtime_value(session, "lease_ttl_minutes"))
    lease_max = int(get_runtime_value(session, "lease_max_minutes"))
    new_expiry = min(
        max(lease.expires_at, now + timedelta(minutes=ttl)),
        now + timedelta(minutes=lease_max),
    )
    lease.expires_at = new_expiry
    lease.state = LeaseState.GRANTED
    if agent_info and auth.credential.agent_label != str(agent_info)[:128]:
        auth.credential.agent_label = str(agent_info)[:128]
    audit.record(
        session, actor=auth.email, action="heartbeat",
        device_id=lease.device_id, lease_id=lease_id,
    )
    session.commit()
    return {
        "lease_id": lease_id, "state": lease.state,
        "last_heartbeat_at": now.isoformat(),
        "expires_at": lease.expires_at.isoformat(),
    }


def release(session: Session, auth: AuthContext, lease_id: str, reason: str | None = None) -> dict:
    lease = session.get(Lease, lease_id)
    if lease is None:
        raise DomainError("NOT_FOUND", f"lease {lease_id} not found")
    if lease.state not in LeaseState.ACTIVE:
        # 幂等：重复释放返回 ok（02 R-B9）
        return {"lease_id": lease_id, "state": lease.state, "ok": True, "idempotent": True}
    if lease.principal_id != auth.principal.id and not auth.is_admin:
        raise DomainError("FORBIDDEN", "只能释放自己的租约")

    _reclaim(session, lease, LeaseState.RELEASED, "released", actor=auth.email)
    session.commit()
    return {"lease_id": lease_id, "state": LeaseState.RELEASED, "ok": True, "reason": reason}


def force_release(session: Session, auth: AuthContext, lease_id: str, reason: str) -> dict:
    """管理员强制回收（02 E 域 R-E5：必须填原因 + 通知原 holder + 落审计）。"""
    if not auth.is_admin:
        raise DomainError("FORBIDDEN", "仅管理员可强制回收")
    if not reason:
        raise DomainError("MISSING_FIELD", "强制回收必须填写原因")
    lease = session.get(Lease, lease_id)
    if lease is None:
        raise DomainError("NOT_FOUND", f"lease {lease_id} not found")
    if lease.state not in LeaseState.ACTIVE:
        return {"lease_id": lease_id, "state": lease.state, "ok": True, "idempotent": True}
    _reclaim(session, lease, LeaseState.FORCED, "forced", actor=auth.email)
    if lease.principal:
        create_notice(
            session,
            "lease_forced",
            title="租约被强制回收",
            body=f"你的租约 {lease_id}（设备 {lease.device_id}）已被管理员强制回收。原因：{reason}",
            recipients=[lease.principal.email],
            device_id=lease.device_id,
            severity="warn",
        )
    session.commit()
    return {"lease_id": lease_id, "state": LeaseState.FORCED, "ok": True}


def force_release_credential_leases(
    session: Session, credential_id: int, actor: str, reason: str = "凭据已吊销"
) -> list[str]:
    """吊销凭据时级联强制回收其名下全部活跃租约（含物理锁清理 + 通知持有人）。

    由凭据吊销流程调用；非活跃租约不处理。返回被回收的 lease_id 列表。
    """
    leases = (
        session.query(Lease)
        .filter(Lease.credential_id == credential_id, Lease.state.in_(LeaseState.ACTIVE))
        .all()
    )
    released: list[str] = []
    for lease in leases:
        holder_email = lease.principal.email if lease.principal else None
        _reclaim(session, lease, LeaseState.FORCED, "credential_revoked", actor=actor)
        if holder_email:
            create_notice(
                session,
                "lease_credential_revoked",
                title="凭据吊销，租约被强制回收",
                body=(
                    f"凭据已吊销，你的租约 {lease.lease_id}（设备 {lease.device_id}）被强制回收。"
                    f"原因：{reason}"
                ),
                recipients=[holder_email],
                device_id=lease.device_id,
                severity="warn",
            )
        released.append(lease.lease_id)
    return released


def my_leases(session: Session, auth: AuthContext) -> list[dict]:
    leases = (
        session.query(Lease)
        .filter(Lease.principal_id == auth.principal.id, Lease.state.in_(LeaseState.ACTIVE))
        .order_by(Lease.created_at)
        .all()
    )
    return [_lease_dict(l) for l in leases]


def ticket(session: Session, auth: AuthContext, ticket_id: str) -> dict:
    entry = session.query(QueueEntry).filter(QueueEntry.entry_id == ticket_id).first()
    if entry is None:
        raise DomainError("NOT_FOUND", f"ticket {ticket_id} not found")
    if entry.principal_id != auth.principal.id and not auth.is_admin:
        raise DomainError("FORBIDDEN", "只能查询自己的票据")

    now = utcnow()
    if entry.state == QueueState.OFFERED:
        if entry.offer_expires_at and entry.offer_expires_at > now:
            # 自动 confirm：RESERVED -> GRANTED（03 §4.3）
            lease = session.query(Lease).filter(Lease.lease_id == entry.lease_id).first()
            if lease is not None and lease.state == LeaseState.RESERVED:
                lease.state = LeaseState.GRANTED
                lease.granted_at = now
                lease.expires_at = now + timedelta(minutes=lease.ttl_minutes)
            entry.state = QueueState.GRANTED
            audit.record(session, actor=auth.email, action="confirm", device_id=lease.device_id if lease else None, lease_id=entry.lease_id)
            session.commit()
            return {"state": "GRANTED", "ticket_id": ticket_id, "lease": _lease_dict(lease) if lease else None}
        else:
            # offer 超时：取消并顺延下一位
            _handle_offer_timeout(session, entry)
            session.commit()
            return {"state": "CANCELLED", "ticket_id": ticket_id, "reason": "offer_timeout"}

    if entry.state == QueueState.QUEUED:
        return {"state": "QUEUED", "ticket_id": ticket_id, "position": position(session, entry)}
    if entry.state == QueueState.GRANTED:
        lease = session.query(Lease).filter(Lease.lease_id == entry.lease_id).first()
        return {"state": "GRANTED", "ticket_id": ticket_id, "lease": _lease_dict(lease) if lease else None}
    return {"state": entry.state, "ticket_id": ticket_id}


def _handle_offer_timeout(session: Session, entry: QueueEntry) -> None:
    lease = session.query(Lease).filter(Lease.lease_id == entry.lease_id).first()
    if lease is not None and lease.state == LeaseState.RESERVED:
        lease.state = LeaseState.RELEASED
        lease.released_at = utcnow()
        lease.end_reason = "offer_timeout"
    entry.state = QueueState.CANCELLED
    if lease is not None:
        device = session.get(Device, lease.device_id)
        if device is not None and device.status != DeviceStatus.PENDING_REMOVAL:
            _wake_queue(session, device)


def ticket_cancel(session: Session, auth: AuthContext, ticket_id: str) -> dict:
    entry = session.query(QueueEntry).filter(QueueEntry.entry_id == ticket_id).first()
    if entry is None:
        raise DomainError("NOT_FOUND", f"ticket {ticket_id} not found")
    if entry.principal_id != auth.principal.id and not auth.is_admin:
        raise DomainError("FORBIDDEN", "只能取消自己的票据")
    if entry.state == QueueState.QUEUED:
        cancel(session, entry, "cancelled")
        session.commit()
    return {"ticket_id": ticket_id, "ok": True, "state": entry.state}


def expire_scan(session: Session) -> dict:
    """租约到期扫描：GRANTED->EXPIRING（宽限开始），EXPIRING 超宽限->EXPIRED 强制回收。"""
    now = utcnow()
    expiring = (
        session.query(Lease)
        .filter(Lease.state == LeaseState.GRANTED, Lease.expires_at.isnot(None), Lease.expires_at <= now)
        .all()
    )
    for l in expiring:
        l.state = LeaseState.EXPIRING
        audit.record(session, actor="system", action="expiring", device_id=l.device_id, lease_id=l.lease_id)

    grace_minutes = int(get_runtime_value(session, "lease_grace_minutes"))
    grace_cutoff = now - timedelta(minutes=grace_minutes)
    expired = (
        session.query(Lease)
        .filter(Lease.state == LeaseState.EXPIRING, Lease.expires_at.isnot(None), Lease.expires_at <= grace_cutoff)
        .all()
    )
    for l in expired:
        _reclaim(session, l, LeaseState.EXPIRED, "expired", actor="system")
        if l.principal:
            create_notice(
                session,
                "lease_expired",
                title="租约超时回收",
                body=f"你的租约 {l.lease_id}（设备 {l.device_id}）已超时并强制回收。",
                recipients=[l.principal.email],
                device_id=l.device_id,
                severity="warn",
            )

    session.commit()
    return {"expiring": len(expiring), "expired": len(expired)}


def heartbeat_scan(session: Session) -> int:
    """心跳超时扫描（v0.18）：超过 interval+grace 未心跳的活跃租约强制回收。

    不依赖 Agent 自觉，由 worker 周期调用；heartbeat_required 关闭时跳过。
    """
    if not bool(get_runtime_value(session, "heartbeat_required")):
        return 0
    now = utcnow()
    interval = int(get_runtime_value(session, "heartbeat_interval_minutes"))
    grace = int(get_runtime_value(session, "heartbeat_miss_grace_minutes"))
    cutoff = now - timedelta(minutes=interval + grace)
    stale = (
        session.query(Lease)
        .filter(
            Lease.state.in_(LeaseState.ACTIVE),
            Lease.last_heartbeat_at.isnot(None),
            Lease.last_heartbeat_at <= cutoff,
        )
        .all()
    )
    for l in stale:
        _reclaim(session, l, LeaseState.EXPIRED, "heartbeat_lost", actor="system")
        if l.principal:
            create_notice(
                session,
                "lease_heartbeat_lost",
                title="心跳超时回收",
                body=f"你的租约 {l.lease_id}（设备 {l.device_id}）因超时未心跳被强制回收。",
                recipients=[l.principal.email],
                device_id=l.device_id,
                severity="warn",
            )
    session.commit()
    return len(stale)


def offer_timeout_scan(session: Session) -> int:
    """offer 确认窗口超时：取消队首、顺延下一位。"""
    now = utcnow()
    offered = (
        session.query(QueueEntry)
        .filter(QueueEntry.state == QueueState.OFFERED, QueueEntry.offer_expires_at.isnot(None), QueueEntry.offer_expires_at <= now)
        .all()
    )
    for entry in offered:
        _handle_offer_timeout(session, entry)
    session.commit()
    return len(offered)
