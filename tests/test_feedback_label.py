"""0.54.15：节点侧反馈可覆盖 agent_label。"""
from __future__ import annotations

from app.domain import feedback
from app.models import AgentFeedback


def test_submit_feedback_agent_label_override(db):
    s = db.session()
    try:
        row = feedback.submit_feedback(
            s, category="question", content="来自 hub 的联调反馈", agent_label="hub:desk-1",
        )
        assert row["agent_label"] == "hub:desk-1"
        saved = s.get(AgentFeedback, row["feedback_id"])
        assert saved is not None and saved.agent_label == "hub:desk-1"
    finally:
        s.close()
