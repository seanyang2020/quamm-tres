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
from app.models import Contact, Credential, Principal
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
