"""Agent 意见收集领域逻辑（v0.20）。

Agent 经 MCP 提交意见；管理员在设置内批判性采纳（accepted/rejected/deferred）。
"""
from __future__ import annotations

import secrets

from app.models import AgentFeedback
from app.utils import utcnow
from app.domain.catalog import DomainError

CATEGORIES = ("bug", "improvement", "question", "praise")
REVIEW_STATUSES = ("accepted", "rejected", "deferred")
_MAX_CONTENT = 4000


def submit_feedback(db, *, credential=None, category: str, content: str) -> dict:
    cat = (category or "").strip().lower()
    if cat not in CATEGORIES:
        raise DomainError("INVALID_CATEGORY", f"category 须为 {', '.join(CATEGORIES)}")
    text = (content or "").strip()
    if not text:
        raise DomainError("EMPTY_CONTENT", "content 不能为空")
    if len(text) > _MAX_CONTENT:
        raise DomainError("CONTENT_TOO_LONG", f"content 不超过 {_MAX_CONTENT} 字")

    row = AgentFeedback(
        feedback_id="fb_" + secrets.token_hex(6),
        credential_id=getattr(credential, "id", None),
        agent_label=getattr(credential, "agent_label", None),
        category=cat,
        content=text,
        status="pending",
        created_at=utcnow(),
    )
    db.add(row)
    db.commit()
    return serialize(row)


def list_feedback(db, status: str | None = None) -> list[dict]:
    q = db.query(AgentFeedback)
    if status:
        q = q.filter(AgentFeedback.status == status)
    rows = q.order_by(AgentFeedback.created_at.desc()).all()
    return [serialize(r) for r in rows]


def review_feedback(db, feedback_id: str, status: str, admin_note: str | None = None) -> dict:
    st = (status or "").strip().lower()
    if st not in REVIEW_STATUSES:
        raise DomainError("INVALID_STATUS", f"status 须为 {', '.join(REVIEW_STATUSES)}")
    row = db.get(AgentFeedback, feedback_id)
    if row is None:
        raise DomainError("NOT_FOUND", "意见不存在")
    row.status = st
    note = (admin_note or "").strip()
    row.admin_note = note or None
    row.reviewed_at = utcnow()
    db.commit()
    return serialize(row)


def serialize(row: AgentFeedback) -> dict:
    return {
        "feedback_id": row.feedback_id,
        "credential_id": row.credential_id,
        "agent_label": row.agent_label,
        "category": row.category,
        "content": row.content,
        "status": row.status,
        "admin_note": row.admin_note,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
    }
