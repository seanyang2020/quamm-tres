"""截图生命周期（v0.24）：记录列举 / 删除 / 保留策略 prune / 存储统计。

只依赖标准库 + SQLAlchemy；磁盘文件以 app.screenshot.SCREENSHOT_DIR 为准。
所有删除都是「删 DB 行 + 删磁盘文件 + 写 audit（action=screenshot_prune，
detail 含删除数量/原因）」三件事一起做，文件缺失静默跳过、不报错。

写库函数末尾显式 session.commit()（项目约定）；commit=False 供更大事务内嵌调用
（leasing._reclaim 用 savepoint 包裹，失败时只回滚清理、不影响释放主流程）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app import screenshot
from app.domain import audit
from app.models import ScreenshotRecord
from app.runtime_config import get_runtime_value
from app.utils import utcnow

logger = logging.getLogger("quamm-tres.screenshot_lifecycle")


def serialize(rec: ScreenshotRecord) -> dict:
    """把 ORM 行转成前端可用的字典；captured_at 带 UTC 偏移，JS 可正确转本地时区。"""
    captured = rec.captured_at
    if captured is not None and captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)
    return {
        "shot_id": rec.shot_id,
        "device_id": rec.device_id,
        "file_name": rec.file_name,
        "bytes": rec.bytes,
        "quality": rec.quality,
        "rotate": rec.rotate,
        "float_abi": rec.float_abi,
        "bin": rec.bin,
        "created_by": rec.created_by,
        "captured_at": captured.isoformat() if captured is not None else None,
    }


def stats(session) -> dict:
    """截图存储统计：总条数 / 占用空间（bytes）/ 涉及设备数。"""
    count = session.query(func.count(ScreenshotRecord.shot_id)).scalar() or 0
    total_bytes = (
        session.query(func.coalesce(func.sum(ScreenshotRecord.bytes), 0)).scalar() or 0
    )
    devices = (
        session.query(func.count(func.distinct(ScreenshotRecord.device_id))).scalar() or 0
    )
    return {"count": int(count), "total_bytes": int(total_bytes), "devices": int(devices)}


def _unlink_file(file_name: str) -> None:
    """best-effort 删磁盘文件：仅接受安全文件名，文件缺失/I/O 错误都不抛。"""
    if not screenshot._SAFE_FILENAME_RE.fullmatch(file_name):
        logger.warning("refuse to unlink unsafe screenshot filename: %r", file_name)
        return
    try:
        (screenshot.SCREENSHOT_DIR / file_name).unlink(missing_ok=True)
    except OSError:
        pass


def _prune_rows(
    session,
    rows: list[ScreenshotRecord],
    *,
    actor: str,
    reason: str,
    device_id: str | None = None,
    commit: bool = True,
) -> int:
    """删给定记录（DB 行 + 文件），有删除时写一条 screenshot_prune 审计；返回删除数。"""
    count = 0
    for rec in rows:
        _unlink_file(rec.file_name)
        session.delete(rec)
        count += 1
    if count:
        audit.record(
            session,
            actor=actor,
            action="screenshot_prune",
            device_id=device_id,
            detail={"count": count, "reason": reason},
        )
    if commit:
        session.commit()
    return count


def delete_device_screenshots(
    session,
    device_id: str,
    *,
    actor: str = "system",
    reason: str = "release",
    commit: bool = True,
) -> int:
    """删除某台设备的全部截图（记录 + 文件），用于换用户/释放时清理。"""
    rows = (
        session.query(ScreenshotRecord)
        .filter(ScreenshotRecord.device_id == device_id)
        .all()
    )
    return _prune_rows(
        session, rows, actor=actor, reason=reason, device_id=device_id, commit=commit
    )


def prune_device(
    session,
    device_id: str,
    *,
    actor: str = "system",
    max_per_device: int | None = None,
) -> int:
    """每次截图后调用：该设备只保留最新 N 条，超出的删记录 + 删文件。"""
    if max_per_device is None:
        max_per_device = int(get_runtime_value(session, "screenshot_max_per_device"))
    rows = (
        session.query(ScreenshotRecord)
        .filter(ScreenshotRecord.device_id == device_id)
        .order_by(ScreenshotRecord.captured_at.desc(), ScreenshotRecord.shot_id.desc())
        .all()
    )
    return _prune_rows(
        session,
        rows[max_per_device:],
        actor=actor,
        reason="per_device_limit",
        device_id=device_id,
    )


def prune_global(session, *, actor: str = "system") -> dict:
    """全局 prune：按 captured_at 删超过 retention_days 的；总数超 total_max 时最旧先删。"""
    retention_days = int(get_runtime_value(session, "screenshot_retention_days"))
    total_max = int(get_runtime_value(session, "screenshot_total_max"))
    cutoff: datetime = utcnow() - timedelta(days=retention_days)

    expired = (
        session.query(ScreenshotRecord)
        .filter(ScreenshotRecord.captured_at < cutoff)
        .order_by(ScreenshotRecord.captured_at.asc())
        .all()
    )
    by_retention = _prune_rows(
        session, expired, actor=actor, reason="retention_days"
    )

    # 保留期删除已 commit，这里重新统计「当前」总数，再按 total_max 删最旧。
    total = session.query(func.count(ScreenshotRecord.shot_id)).scalar() or 0
    by_total = 0
    if total > total_max:
        oldest = (
            session.query(ScreenshotRecord)
            .order_by(ScreenshotRecord.captured_at.asc(), ScreenshotRecord.shot_id.asc())
            .limit(total - total_max)
            .all()
        )
        by_total = _prune_rows(session, oldest, actor=actor, reason="total_max")

    return {
        "retention_deleted": by_retention,
        "total_deleted": by_total,
        "remaining": int(total) - by_total,
    }
