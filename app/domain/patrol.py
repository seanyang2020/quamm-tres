"""巡检与健康（见 02 I 域、11 §2、06 链路 C）。

- 单轮失败标 suspect（health 降级为 L1/L2/L0，status 仍 available，但不再可申请）
- 连续 N 轮失败判 offline，发通知给维护责任人
- 恢复 L3 自动转 available（health 回到 L3/ok），并发"已恢复"通知
- 掉线不改租约（巡检只改健康状态，见 11 §5 边界 2）

巡检按 device.source 分流探测后端（probe.probe_device(source=...)）：
source=mock 走模拟设备 HTTP /probe，source=real 走真实 XOS ADB 文件探测。
可用池口径由 constants.is_claimable 统一：默认只有 status=available 且
health_level=L3 的设备可申请；settings.allow_apply_when_suspect=True 时
额外放行 L1/L2，但 L0 永不进池。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.config import settings
from app.constants import DeviceStatus, HealthLevel, LeaseState
from app.domain import audit
from app.domain.notices import notify_device_offline, notify_device_recovered
from app.domain.probe import probe_device
from app.models import Device, Lease, PatrolRun
from app.utils import new_run_id, utcnow


def _has_active_lease(session: Session, device_id: str) -> bool:
    return (
        session.query(Lease)
        .filter(Lease.device_id == device_id, Lease.state.in_(LeaseState.ACTIVE))
        .first()
        is not None
    )


def run_patrol_round(session: Session, device_ids: list[str] | None = None) -> PatrolRun:
    started = utcnow()
    q = session.query(Device).filter(Device.status != DeviceStatus.RETIRED)
    if device_ids:
        q = q.filter(Device.device_id.in_(device_ids))
    devices = q.all()

    l3 = suspect = offline = 0
    failures: list[dict] = []

    for d in devices:
        if not d.ip:
            continue
        # device 表的 ip/adb_port 即探测地址；端口缺失时按 ip:5555 组。
        # 按 device.source 分流：mock 永远走 HTTP /probe，real 走 adb 读文件。
        result = probe_device(
            d.ip, d.adb_port or 5555,
            timeout=settings.probe_timeout_seconds,
            source=d.source,
        )
        d.last_probe_at = utcnow()

        if result.reachable_l3:
            d.health_level = HealthLevel.L3
            was_offline = d.status == DeviceStatus.OFFLINE
            was_pending = d.status == DeviceStatus.PENDING
            d.consecutive_failures = 0
            d.last_seen_at = utcnow()
            if d.status == DeviceStatus.PENDING:
                d.status = DeviceStatus.AVAILABLE  # 转正（10 §5）
                audit.record(session, actor="system", action="auto_promote", device_id=d.device_id)
            elif was_offline:
                if _has_active_lease(session, d.device_id):
                    d.status = DeviceStatus.OCCUPIED
                else:
                    d.status = DeviceStatus.AVAILABLE
                notify_device_recovered(session, d, d.owner)
                audit.record(session, actor="system", action="recovered", device_id=d.device_id)
            l3 += 1
        else:
            d.health_level = result.level
            d.consecutive_failures += 1
            if d.status == DeviceStatus.PENDING:
                # 仍探不到，保持 pending，不重复计 suspect
                pass
            elif d.status in (DeviceStatus.AVAILABLE, DeviceStatus.OFFLINE):
                if d.consecutive_failures < settings.patrol_fail_threshold:
                    suspect += 1
                else:
                    if d.status != DeviceStatus.OFFLINE:
                        d.status = DeviceStatus.OFFLINE
                        notify_device_offline(session, d, d.owner)
                        audit.record(
                            session, actor="system", action="patrol_offline",
                            device_id=d.device_id, detail={"level": result.level},
                        )
                        offline += 1
                    else:
                        offline += 1
            failures.append({"device_id": d.device_id, "level": result.level, "fails": d.consecutive_failures})

    ended = utcnow()
    run = PatrolRun(
        run_id=new_run_id(),
        started_at=started,
        ended_at=ended,
        duration_ms=int((ended - started).total_seconds() * 1000),
        scanned_count=len(devices),
        l3_count=l3,
        suspect_count=suspect,
        offline_count=offline,
        failures=failures,
        created_at=ended,
    )
    session.add(run)
    session.commit()
    return run
