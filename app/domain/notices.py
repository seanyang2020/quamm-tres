"""通知流水与闭环（见 11 §3）。

一期只落库（notice + notice_receipt），不真正发邮件/企微；真实渠道在 worker 中留 TODO。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.constants import NoticeStatus, NoticeType
from app.models import Notice, NoticeReceipt
from app.utils import new_notice_id, utcnow


def create_notice(
    session: Session,
    event_type: str,
    title: str,
    body: str,
    recipients: list[str],
    device_id: str | None = None,
    severity: str = "info",
    channel: str = "web",
) -> Notice:
    notice = Notice(
        notice_id=new_notice_id(),
        event_type=event_type,
        device_id=device_id,
        title=title,
        body=body,
        severity=severity,
        created_at=utcnow(),
    )
    session.add(notice)
    session.flush()
    for email in recipients:
        session.add(
            NoticeReceipt(
                notice_id=notice.notice_id,
                recipient=email,
                channel=channel,
                status=NoticeStatus.PENDING,
                created_at=utcnow(),
            )
        )
    return notice


def notify_device_offline(session: Session, device, owner_email: str) -> None:
    create_notice(
        session,
        NoticeType.DEVICE_OFFLINE,
        title=f"设备掉线：{device.display_name or device.device_id}",
        body=(
            f"设备 {device.display_name or device.device_id}（位置 {device.position_code or '-'}）"
            f"连续 {device.consecutive_failures} 轮巡检失败，判定为 offline。"
            f"上次在线 {device.last_seen_at}。请排查。"
        ),
        recipients=[owner_email],
        device_id=device.device_id,
        severity="warn",
    )


def notify_device_recovered(session: Session, device, owner_email: str) -> None:
    create_notice(
        session,
        NoticeType.DEVICE_RECOVERED,
        title=f"设备恢复：{device.display_name or device.device_id}",
        body=f"设备 {device.display_name or device.device_id} 已恢复在线，自动转回 available。",
        recipients=[owner_email],
        device_id=device.device_id,
        severity="info",
    )


def notify_device_unregistered(session: Session, device, recipients: list[str]) -> None:
    create_notice(
        session,
        NoticeType.DEVICE_UNREGISTERED,
        title=f"设备已下架：{device.display_name or device.device_id}",
        body=f"你排队等待的设备 {device.display_name or device.device_id} 已下架，请改申请其它同类设备。",
        recipients=recipients,
        device_id=device.device_id,
        severity="warn",
    )
