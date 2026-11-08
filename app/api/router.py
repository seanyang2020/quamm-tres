"""Web 管理 API（:8080，供 Vue 看板调用）。

一期不接管理员登录（权限系统不在范围），接口按管理员视角返回全量数据。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.constants import DeviceStatus, LeaseState, QueueState, is_claimable
from app.db import get_session
from app.domain import catalog, leasing, patrol
from app.models import (
    Credential,
    Device,
    Lease,
    Notice,
    NoticeReceipt,
    PatrolRun,
    Principal,
    ProductType,
    QueueEntry,
)
from app.utils import utcnow

router = APIRouter(prefix="/api")


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


@router.get("/dashboard")
def dashboard(session: Session = Depends(get_session)):
    total = session.query(func.count(Device.device_id)).scalar() or 0
    by_status = dict(
        session.query(Device.status, func.count(Device.device_id)).group_by(Device.status).all()
    )
    # 可用池口径：available 且探测健康（默认仅 L3）。suspect 仍显示 available 状态，
    # 但不可申请，因此 dashboard 的 available 必须按"真实可申请数"回落。
    device_rows = session.query(Device).all()
    available = sum(
        1 for d in device_rows
        if is_claimable(d.status, d.health_level, settings.allow_apply_when_suspect)
    )
    suspect = sum(1 for d in device_rows if d.status == DeviceStatus.AVAILABLE) - available
    by_status["available"] = available
    active_leases = (
        session.query(func.count(Lease.lease_id)).filter(Lease.state.in_(LeaseState.ACTIVE)).scalar() or 0
    )
    queued = (
        session.query(func.count(QueueEntry.entry_id))
        .filter(QueueEntry.state == QueueState.QUEUED)
        .scalar()
        or 0
    )
    products = catalog.list_products(session)
    last_patrol = session.query(PatrolRun).order_by(PatrolRun.ended_at.desc()).first()
    return {
        "total_devices": total,
        "available": available,
        "suspect": suspect,
        "by_status": by_status,
        "allow_apply_when_suspect": settings.allow_apply_when_suspect,
        "active_leases": active_leases,
        "queued": queued,
        "products": products,
        "last_patrol": {
            "run_id": last_patrol.run_id,
            "ended_at": _iso(last_patrol.ended_at),
            "l3_count": last_patrol.l3_count,
            "suspect_count": last_patrol.suspect_count,
            "offline_count": last_patrol.offline_count,
            "scanned_count": last_patrol.scanned_count,
        }
        if last_patrol
        else None,
    }


@router.get("/products")
def products(session: Session = Depends(get_session)):
    return catalog.list_products(session)


@router.get("/devices")
def devices(
    model_class: str | None = None,
    status: str | None = None,
    limit: int = 500,
    session: Session = Depends(get_session),
):
    return catalog.list_devices(session, model_class=model_class, status=status, limit=limit, for_agent=False)


@router.get("/devices/{device_id}")
def device_detail(device_id: str, session: Session = Depends(get_session)):
    try:
        return catalog.device_info(session, device_id, for_agent=False)
    except catalog.DomainError as e:
        raise HTTPException(status_code=404, detail=e.message)


@router.get("/leases")
def leases(session: Session = Depends(get_session)):
    rows = (
        session.query(Lease)
        .filter(Lease.state.in_(LeaseState.ACTIVE))
        .order_by(Lease.created_at.desc())
        .all()
    )
    return [
        {
            "lease_id": l.lease_id,
            "device_id": l.device_id,
            "display_name": l.device.display_name if l.device else None,
            "state": l.state,
            "email": l.principal.email if l.principal else None,
            "purpose": l.purpose,
            "expires_at": _iso(l.expires_at),
            "adb_address": l.adb_address,
        }
        for l in rows
    ]


@router.post("/leases/{lease_id}/force-release")
def force_release(lease_id: str, reason: str, session: Session = Depends(get_session)):
    from app.security import AuthContext

    admin_principal = (
        session.query(Principal).filter(Principal.is_admin.is_(True)).first()
    )
    if admin_principal is None:
        raise HTTPException(status_code=500, detail="no admin principal; run seed first")
    auth = AuthContext(principal=admin_principal, credential=None, email=admin_principal.email, is_admin=True)
    try:
        return leasing.force_release(session, auth, lease_id, reason)
    except catalog.DomainError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.get("/credentials")
def credentials(session: Session = Depends(get_session)):
    rows = session.query(Credential).order_by(Credential.id).all()
    return [
        {
            "id": c.id,
            "email": c.principal.email if c.principal else None,
            "token_prefix": c.token_prefix,
            "expires_at": _iso(c.expires_at),
            "revoked": c.revoked,
            "agent_label": c.agent_label,
            "first_seen_at": _iso(c.first_seen_at),
            "last_used_at": _iso(c.last_used_at),
            "active_leases": (
                session.query(func.count(Lease.lease_id))
                .filter(Lease.credential_id == c.id, Lease.state.in_(LeaseState.ACTIVE))
                .scalar()
                or 0
            ),
        }
        for c in rows
    ]


@router.post("/credentials/{credential_id}/revoke")
def revoke_credential(credential_id: int, session: Session = Depends(get_session)):
    c = session.get(Credential, credential_id)
    if c is None:
        raise HTTPException(status_code=404, detail="credential not found")
    c.revoked_at = utcnow()
    session.commit()
    return {"id": credential_id, "revoked": True}


@router.post("/credentials/renew")
def renew_credential(credential_id: int | None = None, days: int = 90, session: Session = Depends(get_session)):
    """续期不换 token（09 §3）。credential_id 为空则批量续期所有未吊销凭据。"""
    q = session.query(Credential)
    if credential_id is not None:
        q = q.filter(Credential.id == credential_id)
    else:
        q = q.filter(Credential.revoked_at.is_(None))
    count = 0
    for c in q.all():
        base = c.expires_at if c.expires_at and c.expires_at > utcnow() else utcnow()
        c.expires_at = base + timedelta(days=days)
        count += 1
    session.commit()
    return {"renewed": count}


@router.get("/notices")
def notices(session: Session = Depends(get_session)):
    rows = session.query(Notice).order_by(Notice.created_at.desc()).limit(200).all()
    return [
        {
            "notice_id": n.notice_id,
            "event_type": n.event_type,
            "device_id": n.device_id,
            "title": n.title,
            "body": n.body,
            "severity": n.severity,
            "created_at": _iso(n.created_at),
            "receipts": [
                {
                    "recipient": r.recipient,
                    "channel": r.channel,
                    "status": r.status,
                }
                for r in n.receipts
            ],
        }
        for n in rows
    ]


@router.get("/patrol/runs")
def patrol_runs(session: Session = Depends(get_session)):
    rows = session.query(PatrolRun).order_by(PatrolRun.ended_at.desc()).limit(50).all()
    return [
        {
            "run_id": r.run_id,
            "started_at": _iso(r.started_at),
            "ended_at": _iso(r.ended_at),
            "duration_ms": r.duration_ms,
            "scanned_count": r.scanned_count,
            "l3_count": r.l3_count,
            "suspect_count": r.suspect_count,
            "offline_count": r.offline_count,
            "failures": r.failures,
        }
        for r in rows
    ]


@router.get("/health")
def health(session: Session = Depends(get_session)):
    last = session.query(PatrolRun).order_by(PatrolRun.ended_at.desc()).first()
    return {
        "status": "ok",
        "last_patrol_at": _iso(last.ended_at) if last else None,
    }
