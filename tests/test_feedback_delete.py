"""v0.50.0 删除无效 Agent 意见。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.domain import feedback
from app.main import app
from app.models import AgentFeedback, AuditEvent


def test_delete_feedback_removes_row(db):
    s = db.session()
    try:
        row = feedback.submit_feedback(s, category="question", content="无效反馈")
        out = feedback.delete_feedback(s, row["feedback_id"])
        assert out["deleted"] is True
        assert out["feedback_id"] == row["feedback_id"]
        assert s.get(AgentFeedback, row["feedback_id"]) is None
    finally:
        s.close()


def test_delete_missing_feedback_raises_not_found(db):
    s = db.session()
    try:
        with pytest.raises(feedback.DomainError) as ei:
            feedback.delete_feedback(s, "fb_not_exists")
        assert ei.value.code == "NOT_FOUND"
    finally:
        s.close()


def test_delete_feedback_api_admin_and_audit(db):
    s = db.session()
    try:
        row = feedback.submit_feedback(s, category="question", content="过期反馈")
        fid = row["feedback_id"]
    finally:
        s.close()

    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"token": db.token("admin@corp.com")})
        assert login.status_code == 200, login.text
        resp = client.delete(f"/api/feedback/{fid}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] is True

    s = db.session()
    try:
        assert s.get(AgentFeedback, fid) is None
        ev = s.query(AuditEvent).filter(AuditEvent.action == "feedback_delete").first()
        assert ev is not None
        assert ev.detail["feedback_id"] == fid
    finally:
        s.close()
