"""审计：只追加流水（见 02 G 域）。"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import AuditEvent
from app.utils import utcnow


def record(
    session: Session,
    actor: str,
    action: str,
    device_id: str | None = None,
    lease_id: str | None = None,
    detail: dict | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor=actor,
        action=action,
        device_id=device_id,
        lease_id=lease_id,
        detail=detail,
        created_at=utcnow(),
    )
    session.add(event)
    return event
