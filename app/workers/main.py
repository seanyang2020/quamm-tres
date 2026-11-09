"""巡检 worker（独立进程，见 05 §6、06 S12）。

三个定时任务：
- 租约到期扫描（10s）：GRANTED->EXPIRING->EXPIRED 强制回收
- offer 超时扫描（10s）：队首 60s 未确认顺延
- 巡检（默认 30min）：L0-L3 探测，suspect/offline/恢复
生产用 PostgreSQL 时靠 advisory lock 保证单实例（见 05 §7.4），SQLite 单进程演示不重复执行。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler

from app.config import settings
from app.db import SessionLocal, init_db
from app.domain import leasing, patrol, credentials
from app.domain.probe import setup_prober_from_settings
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


def run_worker() -> None:
    init_db()
    with SessionLocal() as session:
        seed_product_types(session)
    setup_prober_from_settings()

    scheduler = BackgroundScheduler()
    scheduler.add_job(_lease_scan_job, "interval", seconds=settings.lease_scan_seconds, id="lease_scan")
    scheduler.add_job(_patrol_job, "interval", minutes=settings.patrol_interval_minutes, id="patrol")
    scheduler.add_job(_credential_expiry_job, "interval", hours=24, id="credential_expiry")
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
