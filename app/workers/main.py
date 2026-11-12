"""巡检 worker（独立进程，见 05 §6、06 S12）。

定时任务：
- 租约到期扫描（10s）：GRANTED->EXPIRING->EXPIRED 强制回收
- offer 超时扫描（10s）：队首 60s 未确认顺延
- 巡检（默认 30min）：L0-L3 探测，suspect/offline/恢复
- 凭据到期提醒（24h）
- 截图全局 prune（1h，v0.24）：按保留天数/总数上限清理，best-effort
生产用 PostgreSQL 时靠 advisory lock 保证单实例（见 05 §7.4），SQLite 单进程演示不重复执行。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db import SessionLocal, init_db
from app.domain import leasing, patrol, credentials
from app.domain.probe import setup_prober_from_settings
from app.constants import DeviceStatus
from app.models import Device
from app.runtime_config import get_runtime_value
from app.utils import utcnow
from app.seed import seed_product_types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("quamm-tres.worker")


def _lease_scan_job() -> None:
    session = SessionLocal()
    try:
        scan = leasing.expire_scan(session)
        offer_timeout = leasing.offer_timeout_scan(session)
        heartbeat_lost = leasing.heartbeat_scan(session)
        if scan.get("expired") or offer_timeout or heartbeat_lost:
            logger.info(
                "lease scan: expiring=%s expired=%s offer_timeout=%s heartbeat_lost=%s",
                scan.get("expiring"), scan.get("expired"), offer_timeout, heartbeat_lost,
            )
    except Exception:  # noqa: BLE001
        logger.exception("lease scan failed")
        session.rollback()
    finally:
        session.close()


def _serial_token_job() -> None:
    """v0.30：租约串口派生票 pending 重试（日志机恢复/心跳上报后补签）。"""
    session = SessionLocal()
    try:
        from app.domain import serial_token

        done = serial_token.retry_pending(session)
        if done:
            logger.info("serial token retry: granted=%s", done)
    except Exception:  # noqa: BLE001
        logger.exception("serial token retry failed")
        session.rollback()
    finally:
        session.close()


def _patrol_job() -> None:
    session = SessionLocal()
    try:
        run = patrol.run_patrol_round(session)
        logger.info(
            "patrol done: scanned=%s l3=%s suspect=%s offline=%s",
            run.scanned_count, run.l3_count, run.suspect_count, run.offline_count,
        )
    except Exception:  # noqa: BLE001
        logger.exception("patrol failed")
        session.rollback()
    finally:
        session.close()


# v0.28：pending 设备专项巡检。用户口径：待检设备每 10 分钟探测一次（可在线调）。
# 每分钟滴答一次，按运行时间隔判定是否该跑，避免改设置要重启 worker。
_last_pending_patrol_at = None


def _pending_patrol_job() -> None:
    global _last_pending_patrol_at
    session = SessionLocal()
    try:
        interval = int(get_runtime_value(session, "pending_patrol_interval_minutes"))
        now = utcnow()
        if _last_pending_patrol_at is not None:
            elapsed = (now - _last_pending_patrol_at).total_seconds()
            if elapsed < max(60, interval * 60 - 5):
                return
        pending_ids = [
            d.device_id
            for d in session.query(Device).filter(Device.status == DeviceStatus.PENDING).all()
        ]
        _last_pending_patrol_at = now
        if not pending_ids:
            return
        run = patrol.run_patrol_round(session, device_ids=pending_ids)
        logger.info(
            "pending patrol done: scanned=%s l3=%s (interval=%smin)",
            run.scanned_count, run.l3_count, interval,
        )
    except Exception:  # noqa: BLE001
        logger.exception("pending patrol failed")
        session.rollback()
    finally:
        session.close()


def _credential_expiry_job() -> None:
    session = SessionLocal()
    try:
        result = credentials.credential_expiry_scan(session)
        if result["notified"]:
            logger.info("credential expiry notice: %s", result)
    except Exception:  # noqa: BLE001
        logger.exception("credential expiry scan failed")
        session.rollback()
    finally:
        session.close()


def _screenshot_prune_job() -> None:
    """v0.24 全局截图 prune（best-effort，不阻塞其它周期任务）。"""
    session = SessionLocal()
    try:
        from app import screenshot_lifecycle

        result = screenshot_lifecycle.prune_global(session, actor="system")
        if result["retention_deleted"] or result["total_deleted"]:
            logger.info("screenshot prune: %s", result)
    except Exception:  # noqa: BLE001
        logger.exception("screenshot prune failed")
        session.rollback()
    finally:
        session.close()


def _cache_prune_job() -> None:
    """v0.28.3 缓存回收：DB 源包/解压目录 + 刷机 downloads/logs（best-effort）。"""
    try:
        from app import cache_prune

        result = cache_prune.prune_all()
        logger.info(
            "cache prune: freed=%s (%s -> %s)",
            result["freed_human"], result["before_total_human"], result["after_total_human"],
        )
    except Exception:  # noqa: BLE001
        logger.exception("cache prune failed")


def run_worker() -> None:
    init_db()
    with SessionLocal() as session:
        # v0.30：worker 可能先于 API 启动，幂等补列后再跑扫描
        from app.migrations import migrate

        migrate(session)
        seed_product_types(session)
    with SessionLocal() as session:
        setup_prober_from_settings(session)

    scheduler = BackgroundScheduler()
    scheduler.add_job(_lease_scan_job, "interval", seconds=settings.lease_scan_seconds, id="lease_scan")
    scheduler.add_job(_patrol_job, "interval", minutes=settings.patrol_interval_minutes, id="patrol")
    # v0.28：待检设备每 10 分钟探测一次（运行时 pending_patrol_interval_minutes 可调）
    scheduler.add_job(_pending_patrol_job, "interval", minutes=1, id="pending_patrol")
    # v0.30：串口派生票 pending 重试（每分钟滴答，命中量通常为 0）
    scheduler.add_job(_serial_token_job, "interval", seconds=60, id="serial_token_retry")
    scheduler.add_job(_credential_expiry_job, "interval", hours=24, id="credential_expiry")
    # v0.24：截图全局保留策略（每小时一次，best-effort，独立线程不阻塞其它任务）
    scheduler.add_job(_screenshot_prune_job, "interval", hours=1, id="screenshot_prune")
    # v0.28.3：缓存回收（默认 6 小时一次；DB 源包/解压目录、刷机下载/日志）
    scheduler.add_job(
        _cache_prune_job, "interval",
        hours=max(1, settings.cache_prune_interval_hours), id="cache_prune",
    )
    scheduler.start()
    logger.info("worker started (lease_scan=%ss, patrol=%smin)", settings.lease_scan_seconds, settings.patrol_interval_minutes)

    # 重启后立即对账：DB 是状态唯一事实源，停机期间到期/心跳超时的租约立刻回收，
    # 有效租约保持 GRANTED；不依赖 TTL 猜测或 Agent 重新上报。
    try:
        _lease_scan_job()
        logger.info("startup reconciliation done")
    except Exception:  # noqa: BLE001
        logger.exception("startup reconciliation failed")

    try:
        import time

        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    run_worker()
