"""数据库引擎与会话工厂。

本地开发 SQLite，生产 PostgreSQL 16（通过 RES_DATABASE_URL 覆盖）。
`SessionLocal` 是函数而非固定 sessionmaker：测试可调用 configure() 指向临时库。
"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def _make_engine(url: str):
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        # 允许跨线程（worker / 多线程测试），设置 busy timeout 避免并发写锁冲突
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    engine = create_engine(url, **kwargs)

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return engine


_engine = None
_session_local: sessionmaker | None = None


def configure(url: str | None = None):
    """（重新）配置全局引擎与会话工厂。测试用它指向临时 SQLite。"""
    global _engine, _session_local
    _engine = _make_engine(url or settings.database_url)
    _session_local = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_engine():
    if _engine is None:
        configure()
    return _engine


def get_session_factory() -> sessionmaker:
    if _session_local is None:
        configure()
    return _session_local


def SessionLocal() -> Session:
    """创建会话（函数形式，动态取当前工厂）。"""
    return get_session_factory()()


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def init_db() -> None:
    """建表（幂等）。"""
    from app import models  # noqa: F401 确保模型注册到 Base

    Base.metadata.create_all(bind=get_engine())
