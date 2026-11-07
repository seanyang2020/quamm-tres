"""巡检：单轮 suspect -> 连续 2 轮 offline + notice -> 恢复 available。"""
from __future__ import annotations

from app.constants import DeviceStatus, HealthLevel
from app.domain.patrol import run_patrol_round
from app.models import Device, Notice, NoticeReceipt

from tests.conftest import register_device
from tests.mock_device import MockDevice


def test_patrol_suspect_offline_recover(db):
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "302-R6-01", "owner@corp.com")
        device_id = reg["device_id"]
        assert reg["status"] == DeviceStatus.AVAILABLE

        # 掉线 -> 单轮 suspect（不通知）
        dev.set_level("L1")
        run_patrol_round(db.session())
        s = db.session()
        d = s.get(Device, device_id)
        assert d.consecutive_failures == 1
        assert d.status == DeviceStatus.AVAILABLE  # 仍 available，但 health 降级
        assert d.health_level == HealthLevel.L1
        notices = s.query(Notice).count()
        assert notices == 0, "单轮失败不应产生通知"
        s.close()

        # 连续 2 轮 -> offline + notice
        run_patrol_round(db.session())
        s = db.session()
        d = s.get(Device, device_id)
        assert d.consecutive_failures == 2
        assert d.status == DeviceStatus.OFFLINE
        notice = s.query(Notice).filter(Notice.device_id == device_id).first()
        assert notice is not None
        assert notice.event_type == "device_offline"
        receipt = s.query(NoticeReceipt).filter(NoticeReceipt.notice_id == notice.notice_id).first()
        assert receipt.recipient == "owner@corp.com"
        s.close()

        # 恢复 -> 自动转 available + 已恢复通知
        dev.set_level("L3")
        run_patrol_round(db.session())
        s = db.session()
        d = s.get(Device, device_id)
        assert d.status == DeviceStatus.AVAILABLE
        assert d.health_level == HealthLevel.L3
        assert d.consecutive_failures == 0
        recovered = (
            s.query(Notice)
            .filter(Notice.device_id == device_id, Notice.event_type == "device_recovered")
            .first()
        )
        assert recovered is not None
        s.close()
    finally:
        dev.stop()


def test_offline_notice_only_once(db):
    """已 offline 的设备再失败不重复发通知。"""
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "302-R6-02", "owner@corp.com")
        device_id = reg["device_id"]
        dev.set_level("L1")
        run_patrol_round(db.session())
        run_patrol_round(db.session())  # offline + notice
        run_patrol_round(db.session())  # 仍 offline，不应再发

        s = db.session()
        cnt = (
            s.query(Notice)
            .filter(Notice.device_id == device_id, Notice.event_type == "device_offline")
            .count()
        )
        assert cnt == 1, cnt
        s.close()
    finally:
        dev.stop()
