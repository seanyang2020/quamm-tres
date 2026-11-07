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
from app.domain import leasing, patrol
from app.domain.probe import setup_prober_from_settings
from app.seed import seed_product_types

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("quamm-res.worker")


def _lease_scan_job() -> None:
    session = SessionLocal()
    try:
        expiring, expired = leasing.expire_scan(session)
        offer_timeout = leasing.offer_timeout_scan(session)
        if expired or offer_timeout:
            logger.info("lease scan: expiring=%s expired=%s offer_timeout=%s", expiring, expired, offer_timeout)
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


def run_worker() -> None:
    init_db()
    with SessionLocal() as session:
        seed_product_types(session)
    setup_prober_from_settings()

    scheduler = BackgroundScheduler()
    scheduler.add_job(_lease_scan_job, "interval", seconds=settings.lease_scan_seconds, id="lease_scan")
    scheduler.add_job(_patrol_job, "interval", minutes=settings.patrol_interval_minutes, id="patrol")
    scheduler.start()
    logger.info("worker started (lease_scan=%ss, patrol=%smin)", settings.lease_scan_seconds, settings.patrol_interval_minutes)

    try:
        import time

        while True:
            time.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()


if __name__ == "__main__":
    run_worker()
