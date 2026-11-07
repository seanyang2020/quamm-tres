"""排队（见 02 C 域、11 §7.3）。

排序：priority 降序 → 入队时间升序（严格 FIFO）。
"""
from __future__ import annotations

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.constants import QueueState
from app.models import Principal, QueueEntry
from app.utils import new_ticket_id, utcnow


def enqueue(
    session: Session,
    principal: Principal,
    credential_id: int | None,
    target_type: str,
    target_code: str,
    purpose: str | None,
    priority: int = 0,
) -> QueueEntry:
    entry = QueueEntry(
        entry_id=new_ticket_id(),
        target_type=target_type,
        target_code=target_code,
        principal_id=principal.id,
        credential_id=credential_id,
        purpose=purpose,
        priority=priority,
        state=QueueState.QUEUED,
        joined_at=utcnow(),
        created_at=utcnow(),
    )
    session.add(entry)
    session.flush()
    return entry


def position(session: Session, entry: QueueEntry) -> int:
    """当前位次：同目标、同状态里排在第几（priority 降序 + joined_at 升序）。"""
    q = (
        session.query(QueueEntry)
        .filter(
            and_(
                QueueEntry.target_type == entry.target_type,
                QueueEntry.target_code == entry.target_code,
                QueueEntry.state == QueueState.QUEUED,
            )
        )
    )
    count = 0
    for e in q.order_by(QueueEntry.priority.desc(), QueueEntry.joined_at.asc()).all():
        count += 1
        if e.entry_id == entry.entry_id:
            return count
    return 0


def find_head(session: Session, target_type: str, target_code: str) -> QueueEntry | None:
    """返回队首（仍 QUEUED 的最高优先级、最早入队）。"""
    return (
        session.query(QueueEntry)
        .filter(
            and_(
                QueueEntry.target_type == target_type,
                QueueEntry.target_code == target_code,
                QueueEntry.state == QueueState.QUEUED,
            )
        )
        .order_by(QueueEntry.priority.desc(), QueueEntry.joined_at.asc())
        .first()
    )


def cancel(session: Session, entry: QueueEntry, reason: str) -> None:
    entry.state = QueueState.CANCELLED
