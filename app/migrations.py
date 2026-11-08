"""轻量数据迁移（幂等，SQLite/PG 通用）。

0.10.0 引入：principal 表新增 ``is_admin`` 布尔列，且仅 ``admin@corp.com`` 为管理员。
早期库可能缺该列（模型更早版本没有），也可能把 user01 误标成管理员，这里一并修正，
不删除任何已有记录。
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.seed import ADMIN_EMAIL


def migrate(session: Session) -> None:
    """幂等迁移：补 is_admin 列 + 修正管理员标志。"""
    _ensure_is_admin_column(session)
    _fix_admin_flags(session)


def _ensure_is_admin_column(session: Session) -> None:
    engine = session.get_bind()
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("principal")}
    if "is_admin" not in columns:
        # SQLite ALTER TABLE ADD COLUMN（既有行默认 0 / false）
        session.execute(
            text("ALTER TABLE principal ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0")
        )
        session.commit()


def _fix_admin_flags(session: Session) -> None:
    session.execute(
        text("UPDATE principal SET is_admin = 0 WHERE email != :admin"),
        {"admin": ADMIN_EMAIL},
    )
    session.execute(
        text("UPDATE principal SET is_admin = 1 WHERE email = :admin"),
        {"admin": ADMIN_EMAIL},
    )
    session.commit()
