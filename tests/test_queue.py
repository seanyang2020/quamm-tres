"""排队：并发抢同一台 -> 排队 -> 释放 -> 队首 offer -> 确认 / 超时顺延。"""
from __future__ import annotations

import threading
from datetime import timedelta

from app.constants import DeviceStatus, LeaseState, QueueState
from app.domain import leasing
from app.mcp.server import res_apply, res_release, res_ticket
from app.models import Device, Lease, QueueEntry
from app.utils import utcnow

from tests.conftest import register_device
from tests.mock_device import MockDevice


def _register_one(db, model="QM102V"):
    dev = MockDevice(model=model).start()
    reg = register_device(db, "user01@corp.com", dev, "302-R1-01", "owner@corp.com")
    return dev, reg["device_id"]


def test_concurrent_apply_same_device_one_granted_one_queued(db):
    dev, device_id = _register_one(db)
    try:
        results = {}
        lock = threading.Lock()

        def worker(email):
            r = db.call(email, res_apply, target=device_id, purpose="抢")
            with lock:
                results[email] = r

        t1 = threading.Thread(target=worker, args=("user02@corp.com",))
        t2 = threading.Thread(target=worker, args=("user03@corp.com",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        states = sorted(r["state"] for r in results.values())
        assert states == ["GRANTED", "QUEUED"], results

        # 恰好一个 GRANTED，无重复授予
        s = db.session()
        active = (
            s.query(Lease)
            .filter(Lease.device_id == device_id, Lease.state.in_(LeaseState.ACTIVE))
            .count()
        )
        s.close()
        assert active == 1
    finally:
        dev.stop()


def test_release_wakes_queue_head_offer_and_confirm(db):
    dev, device_id = _register_one(db)
    try:
        r1 = db.call("user02@corp.com", res_apply, target=device_id)
        assert r1["state"] == "GRANTED"
        lease_id = r1["lease"]["lease_id"]

        r2 = db.call("user03@corp.com", res_apply, target=device_id)
        assert r2["state"] == "QUEUED"
        ticket_id = r2["ticket_id"]

        # 释放 -> 队首收到 offer（RESERVED）
        db.call("user02@corp.com", res_release, lease_id=lease_id)
        s = db.session()
        entry = s.query(QueueEntry).filter(QueueEntry.entry_id == ticket_id).first()
        assert entry.state == QueueState.OFFERED, entry.state
        s.close()

        # 队首调 res_ticket -> 自动 confirm -> GRANTED
        t = db.call("user03@corp.com", res_ticket, ticket_id=ticket_id)
        assert t["state"] == "GRANTED", t
        assert t["lease"]["adb_address"] == dev.adb_address
    finally:
        dev.stop()


def test_offer_timeout_passes_to_next(db):
    dev, device_id = _register_one(db)
    try:
        r1 = db.call("user02@corp.com", res_apply, target=device_id)
        assert r1["state"] == "GRANTED"
        lease_id = r1["lease"]["lease_id"]

        t2 = db.call("user03@corp.com", res_apply, target=device_id)["ticket_id"]
        t3 = db.call("user04@corp.com", res_apply, target=device_id)["ticket_id"]

        # 释放 -> 队首 user03 收到 offer
        db.call("user02@corp.com", res_release, lease_id=lease_id)

        s = db.session()
        e2 = s.query(QueueEntry).filter(QueueEntry.entry_id == t2).first()
        assert e2.state == QueueState.OFFERED

        # 模拟 offer 超时
        e2.offer_expires_at = utcnow() - timedelta(seconds=1)
        s.commit()
        n = leasing.offer_timeout_scan(s)
        assert n == 1
        s.close()

        s = db.session()
        e2 = s.query(QueueEntry).filter(QueueEntry.entry_id == t2).first()
        e3 = s.query(QueueEntry).filter(QueueEntry.entry_id == t3).first()
        assert e2.state == QueueState.CANCELLED, e2.state
        assert e3.state == QueueState.OFFERED, e3.state  # 顺延到 user04
        s.close()

        # user04 确认拿到
        t = db.call("user04@corp.com", res_ticket, ticket_id=t3)
        assert t["state"] == "GRANTED", t
    finally:
        dev.stop()


def test_cancel_ticket(db):
    dev, device_id = _register_one(db)
    try:
        r1 = db.call("user02@corp.com", res_apply, target=device_id)
        assert r1["state"] == "GRANTED"
        t2 = db.call("user03@corp.com", res_apply, target=device_id)["ticket_id"]

        from app.mcp.server import res_ticket_cancel

        c = db.call("user03@corp.com", res_ticket_cancel, ticket_id=t2)
        assert c["ok"] is True
    finally:
        dev.stop()
