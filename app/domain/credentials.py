"""凭据发放（MCP 邀请）：为任意指定邮箱生成 admin/user token，可直接 SMTP 发邀请函。

口径：
- 邮箱可任意指定（不限制通讯录），缺失则自动建 Principal/Contact。
- 目标邮箱若已有有效凭据，默认不补发，返回 needs_confirm；显式 confirm=true 才再发一枚。
- 每次发放写审计；角色仅落在 Credential.role，与 Web 批量发放保持一致。
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app import mailer
from app.domain import audit
from app.domain.catalog import DomainError
from app.domain.notices import create_notice
from app.models import Contact, Credential, Notice, Principal
from app.runtime_config import get_runtime_value
from app.utils import hash_token, new_token, token_prefix, utcnow


def issue_credential(
    session: Session,
    email: str,
    role: str = "user",
    name: str | None = None,
    note: str | None = None,
    valid_days: int | None = None,
    actor_email: str | None = None,
    confirm: bool = False,
    send: bool = False,
) -> dict:
    email = (email or "").strip().lower()
    if "@" not in email:
        raise DomainError("BAD_EMAIL", "邮箱格式无效")
    if role not in ("admin", "user"):
        role = "user"

    now = utcnow()
    default_days = int(get_runtime_value(session, "credential_valid_days", 90))
    if valid_days is None:
        days = default_days
    else:
        try:
            days = int(valid_days)
        except (TypeError, ValueError):
            days = default_days
    days = max(1, min(days, 3650))

    principal = session.query(Principal).filter(Principal.email == email).first()

    if principal is not None:
        active_creds = [c for c in principal.credentials if c.active]
        if active_creds and not confirm:
            return {
                "needs_confirm": True,
                "email": email,
                "active_credentials": [
                    {
                        "id": c.id,
                        "role": c.role,
                        "token_prefix": c.token_prefix,
                        "expires_at": c.expires_at.isoformat(),
                    }
                    for c in active_creds
                ],
                "message": "该邮箱已有有效凭据；确认补发请带 confirm=true 再调用",
            }

    if principal is None:
        quota = int(get_runtime_value(session, "holder_concurrency_limit", 2))
        principal = Principal(
            email=email,
            name=name or email,
            employee_no=None,
            quota=quota,
            is_admin=False,
            status="active",
            created_at=now,
        )
        session.add(principal)
        session.flush()
    elif name and not principal.name:
        principal.name = name

    if session.get(Contact, email) is None:
        session.add(Contact(email=email, name=name or principal.name, note=note, created_at=now))

    token = new_token()
    cred = Credential(
        principal_id=principal.id,
        token_hash=hash_token(token),
        token_prefix=token_prefix(token),
        expires_at=now + timedelta(days=days),
        role=role,
        note=note,
        created_at=now,
    )
    session.add(cred)
    audit.record(
        session,
        actor=actor_email or email,
        action="issue_credential",
        detail={"target": email, "role": role, "send": send, "confirm": confirm},
    )
    session.commit()

    result = {
        "needs_confirm": False,
        "email": email,
        "name": principal.name,
        "role": role,
        "token": token,
        "valid_days": days,
        "expires_at": cred.expires_at.isoformat(),
        "subject": mailer.CREDENTIAL_SUBJECT,
        "body": mailer.build_credential_body(token, note),
        "mcp_config": mailer.build_mcp_config(token),
        "mcp_config_text": mailer.mcp_config_text(token),
    }

    if send:
        try:
            mailer.send_mail(
                session,
                email,
                mailer.CREDENTIAL_SUBJECT,
                mailer.build_credential_body(token, note),
                cfg=mailer.get_smtp_config(session),
            )
            result["sent"] = True
        except Exception as e:  # noqa: BLE001 - 发信失败不影响 token 有效性
            result["sent"] = False
            result["mail_error"] = str(e)

    return result


def credential_expiry_scan(session: Session, warn_days: int = 7) -> dict:
    """扫描即将到期（默认 7 天内）的有效凭据，通知持有人与管理员。

    24 小时内对同一凭据已发过到期提醒则跳过（避免重复通知）；
    已吊销凭据不参与。worker 每日跑一次。
    """
    now = utcnow()
    horizon = now + timedelta(days=warn_days)
    creds = (
        session.query(Credential)
        .filter(Credential.revoked_at.is_(None), Credential.expires_at <= horizon)
        .all()
    )
    admin_emails = [
        p.email for p in session.query(Principal).filter(Principal.is_admin.is_(True)).all()
    ]
    notified, skipped = [], []
    for c in creds:
        marker = f"credential:{c.id}"
        recent = (
            session.query(Notice)
            .filter(
                Notice.event_type == "credential_expiring",
                Notice.created_at >= now - timedelta(hours=24),
                Notice.body.contains(marker),
            )
            .first()
        )
        if recent is not None:
            skipped.append(c.id)
            continue
        holder = c.principal.email if c.principal else None
        recipients = list(dict.fromkeys([*( [holder] if holder else []), *admin_emails]))
        days_left = max(0, (c.expires_at - now).days)
        create_notice(
            session,
            "credential_expiring",
            title=f"凭据即将到期：{c.token_prefix}（{days_left} 天后）",
            body=(
                f"{marker} 凭据 {c.token_prefix}（角色 {c.role}）将于 {c.expires_at.isoformat()} 到期，"
                f"剩余约 {days_left} 天。请联系管理员重新发放，到期后 MCP/Web 将无法继续使用。"
            ),
            recipients=recipients,
            severity="warn",
        )
        notified.append(c.id)
    session.commit()
    return {"warn_days": warn_days, "notified": notified, "skipped": skipped, "checked": len(creds)}


def revoke_credential(session: Session, credential_id: int, actor: str, reason: str = "管理员吊销") -> dict:
    """吊销凭据并级联强制回收其名下活跃租约（含物理锁清理）。已吊销则幂等返回。"""
    from app.domain import leasing

    cred = session.get(Credential, credential_id)
    if cred is None:
        raise DomainError("NOT_FOUND", f"credential {credential_id} not found")
    if cred.revoked:
        return {"id": credential_id, "revoked": True, "released": [], "idempotent": True}
    cred.revoked_at = utcnow()
    released = leasing.force_release_credential_leases(session, credential_id, actor=actor, reason=reason)
    audit.record(
        session,
        actor=actor,
        action="revoke_credential",
        detail={"credential_id": credential_id, "target": cred.principal.email if cred.principal else None,
                "released_leases": released},
    )
    session.commit()
    return {"id": credential_id, "revoked": True, "released": released}


def restore_credential(session: Session, credential_id: int, actor: str) -> dict:
    """恢复已吊销凭据（清空 revoked_at）。未吊销则幂等返回。"""
    cred = session.get(Credential, credential_id)
    if cred is None:
        raise DomainError("NOT_FOUND", f"credential {credential_id} not found")
    if not cred.revoked:
        return {"id": credential_id, "revoked": False, "idempotent": True}
    cred.revoked_at = None
    audit.record(
        session,
        actor=actor,
        action="restore_credential",
        detail={"credential_id": credential_id, "target": cred.principal.email if cred.principal else None},
    )
    session.commit()
    return {"id": credential_id, "revoked": False}


def delete_credential(session: Session, credential_id: int, actor: str) -> dict:
    """硬删除凭据。若仍有任何租约引用（FK）则拒绝，需先保留/清理历史租约。"""
    from app.models import Lease

    cred = session.get(Credential, credential_id)
    if cred is None:
        raise DomainError("NOT_FOUND", f"credential {credential_id} not found")
    lease_count = session.query(Lease).filter(Lease.credential_id == credential_id).count()
    if lease_count:
        raise DomainError(
            "CREDENTIAL_IN_USE",
            f"凭据仍有 {lease_count} 条租约记录引用，无法删除（为保留历史不允许硬删）",
            {"lease_count": lease_count},
        )
    target = cred.principal.email if cred.principal else None
    session.delete(cred)
    audit.record(
        session,
        actor=actor,
        action="delete_credential",
        detail={"credential_id": credential_id, "target": target},
    )
    session.commit()
    return {"id": credential_id, "deleted": True}


def force_delete_credential(session: Session, credential_id: int, actor: str) -> dict:
    """管理员强制删除：先回收活跃租约，再硬删除该凭据及其全部关联数据（租约/排队条目）。

    与 delete_credential 的区别：不做引用保护，关联数据一并清除，操作全程留审计。
    """
    from app.models import Lease, QueueEntry
    from app.domain import leasing

    cred = session.get(Credential, credential_id)
    if cred is None:
        raise DomainError("NOT_FOUND", f"credential {credential_id} not found")
    target = cred.principal.email if cred.principal else None

    # 1) 先把活跃租约正确回收（释放设备、清理物理锁、通知），再删除全部租约
    reclaimed = leasing.force_release_credential_leases(
        session, credential_id, actor=actor, reason="管理员强制删除凭据"
    )
    lease_count = session.query(Lease).filter(Lease.credential_id == credential_id).count()
    session.query(Lease).filter(Lease.credential_id == credential_id).delete(
        synchronize_session=False
    )
    # 2) 删除该凭据关联的排队条目
    queue_count = (
        session.query(QueueEntry).filter(QueueEntry.credential_id == credential_id).count()
    )
    session.query(QueueEntry).filter(QueueEntry.credential_id == credential_id).delete(
        synchronize_session=False
    )
    # 3) 删除凭据本身
    session.delete(cred)
    audit.record(
        session,
        actor=actor,
        action="force_delete_credential",
        detail={
            "credential_id": credential_id,
            "target": target,
            "reclaimed_active_leases": reclaimed,
            "deleted_leases": lease_count,
            "deleted_queue_entries": queue_count,
        },
    )
    session.commit()
    return {
        "id": credential_id,
        "force_deleted": True,
        "deleted_leases": lease_count,
        "deleted_queue_entries": queue_count,
    }
