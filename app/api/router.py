"""Web 管理 API（:9100，供 Vue 看板调用）。

v0.11 起接入登录：rr_ token 可在 /api/auth/login 换 HttpOnly 会话 cookie。
- 只读展示接口：optional_identity，可匿名（历史验收脚本直接打真服务，保持 200）；
  登录后 user 只看自己的租约/申请/凭据，admin 看全部。
- 写操作 / 通讯录 / SMTP / 发凭据 / 引导：require_admin（未登录 401，user 403）。
- MCP 端仍走 Authorization header，完全不受 cookie 影响。
"""
from __future__ import annotations

import threading
from datetime import timedelta

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import mailer, web_auth
from app.config import settings
from app.constants import (
    PENDING_CLEANUP_DAYS,
    DeviceStatus,
    HealthLevel,
    LeaseState,
    QueueState,
    get_version,
    is_claimable,
    version_info,
)
from app.db import get_session
from app.domain import catalog, credentials, leasing, patrol
from app.domain.queueing import position as queue_position
from app.models import (
    AuditEvent,
    Contact,
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
from app.runtime_config import (
    RUNTIME_KEYS,
    get_runtime_value,
    runtime_public_view,
    set_runtime_value,
    validate_runtime_value,
)
from app.security import AuthError, resolve_token
from app.utils import hash_token, new_id, new_token, token_prefix, utcnow

router = APIRouter(prefix="/api")


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _contact_name(session: Session, email: str | None) -> str | None:
    if not email:
        return None
    c = session.get(Contact, email)
    return c.name if c and c.name else None


def _admin_token_count(session: Session) -> int:
    """当前是否存在可用的 role=admin 凭据（引导只在 0 条时触发）。"""
    return (
        session.query(func.count(Credential.id))
        .filter(Credential.role == "admin", Credential.revoked_at.is_(None))
        .scalar()
        or 0
    )


def _is_user_view(identity) -> bool:
    """是否为普通用户视角。直接调用路由函数（pytest 等）时 identity 是 Depends 对象，按全量处理。"""
    return isinstance(identity, web_auth.WebIdentity) and identity.role != "admin"


# ---------- 登录认证（v0.11） ----------

@router.get("/version")
def version():
    # 版本号与 name 是稳定 API 契约（历史验收脚本断言该值），保持公开且不变。
    return version_info()


@router.get("/auth/setup-status")
def auth_setup_status(session: Session = Depends(get_session)):
    """公开：Web 打开时先判断要不要走首次引导。已有 admin token 时为 False。"""
    return {"needs_setup": _admin_token_count(session) == 0}


@router.post("/auth/setup")
def auth_setup(payload: dict = Body(...), session: Session = Depends(get_session)):
    """首次引导：库里没有任何 role=admin 的 token 时，凭邮箱生成第一个 admin token。"""
    if _admin_token_count(session) > 0:
        raise HTTPException(status_code=409, detail="已存在管理员，引导不可用")
    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱不合法")
    name = str(payload.get("name") or email).strip()
    now = utcnow()

    principal = session.query(Principal).filter(Principal.email == email).first()
    if principal is None:
        principal = Principal(
            email=email,
            name=name,
            employee_no=None,
            quota=int(get_runtime_value(session, "holder_concurrency_limit")),
            is_admin=True,
            status="active",
            created_at=now,
        )
        session.add(principal)
        session.flush()
    else:
        principal.is_admin = True
        principal.status = "active"

    if session.get(Contact, email) is None:
        session.add(Contact(email=email, name=name, note="首次引导创建", created_at=now))

    token = new_token()
    valid_days = int(get_runtime_value(session, "credential_valid_days"))
    session.add(
        Credential(
            principal_id=principal.id,
            token_hash=hash_token(token),
            token_prefix=token_prefix(token),
            expires_at=now + timedelta(days=valid_days),
            role="admin",
            note="首次引导创建",
            created_at=now,
        )
    )
    session.commit()
    return {"email": email, "name": name, "role": "admin", "token": token}


@router.post("/auth/login")
def auth_login(
    response: Response,
    payload: dict = Body(...),
    session: Session = Depends(get_session),
):
    """用 rr_ token 换 8 小时 HttpOnly 会话 cookie；token 校验复用 MCP 同一套逻辑。"""
    token = str(payload.get("token") or "").strip()
    if not token.startswith(settings.token_prefix):
        raise HTTPException(status_code=401, detail="无效的 rr_ token")
    try:
        auth = resolve_token(session, token)
    except AuthError as e:
        raise HTTPException(status_code=401, detail=e.message)

    role = "admin" if (auth.credential.role == "admin" or auth.principal.is_admin) else "user"
    name = auth.principal.name or _contact_name(session, auth.principal.email)
    response.set_cookie(
        web_auth.SESSION_COOKIE,
        web_auth.make_session(auth.credential.id),
        max_age=settings.web_session_hours * 3600,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return {"email": auth.principal.email, "name": name, "role": role}


@router.get("/auth/me")
def auth_me(identity: web_auth.WebIdentity = Depends(web_auth.require_login)):
    return {"email": identity.email, "name": identity.name, "role": identity.role}


@router.post("/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(web_auth.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/dashboard")
def dashboard(
    identity: web_auth.WebIdentity | None = Depends(web_auth.optional_identity),
    session: Session = Depends(get_session),
):
    total = session.query(func.count(Device.device_id)).scalar() or 0
    by_status = dict(
        session.query(Device.status, func.count(Device.device_id)).group_by(Device.status).all()
    )
    # 可用池口径：available 且探测健康（默认仅 L3）。suspect 仍显示 available 状态，
    # 但不可申请，因此 dashboard 的 available 必须按"真实可申请数"回落。
    device_rows = session.query(Device).all()
    allow_suspect = bool(get_runtime_value(session, "allow_apply_when_suspect"))
    available = sum(
        1 for d in device_rows
        if is_claimable(d.status, d.health_level, allow_suspect)
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
    # 工程配置名（project_name）分布：真实设备一眼可见属于哪个工程配置。
    project_rows = (
        session.query(Device.project_name, func.count(Device.device_id))
        .group_by(Device.project_name)
        .all()
    )
    projects = [
        {"project_name": pn or "-", "count": cnt} for pn, cnt in project_rows
    ]
    last_patrol = session.query(PatrolRun).order_by(PatrolRun.ended_at.desc()).first()
    result = {
        "total_devices": total,
        "available": available,
        "suspect": suspect,
        "by_status": by_status,
        "projects": projects,
        "allow_apply_when_suspect": allow_suspect,
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
    # 普通用户看得到可用池，但看不到「谁在用 / 占用与排队详情」。
    if _is_user_view(identity):
        result["by_status"] = {"available": available}
        result["active_leases"] = None
        result["queued"] = None
    return result


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
def leases(
    identity: web_auth.WebIdentity | None = Depends(web_auth.optional_identity),
    session: Session = Depends(get_session),
):
    q = (
        session.query(Lease)
        .filter(Lease.state.in_(LeaseState.ACTIVE))
        .order_by(Lease.created_at.desc())
    )
    if _is_user_view(identity):
        q = q.filter(Lease.principal_id == identity.principal_id)
    rows = q.all()
    return [
        {
            "lease_id": l.lease_id,
            "device_id": l.device_id,
            "display_name": l.device.display_name if l.device else None,
            "state": l.state,
            "email": l.principal.email if l.principal else None,
            # v0.11：从 token 关联到联系人，显示申请人姓名而非仅 token 前缀。
            "name": (
                _contact_name(session, l.principal.email)
                or (l.principal.name if l.principal else None)
            ),
            "purpose": l.purpose,
            "expires_at": _iso(l.expires_at),
            "adb_address": l.adb_address,
            "agent_label": l.credential.agent_label if l.credential else None,
        }
        for l in rows
    ]


@router.post("/leases/{lease_id}/force-release")
def force_release(
    lease_id: str,
    reason: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
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
def credentials(
    identity: web_auth.WebIdentity | None = Depends(web_auth.optional_identity),
    session: Session = Depends(get_session),
):
    q = session.query(Credential).order_by(Credential.id)
    if _is_user_view(identity):
        q = q.filter(Credential.principal_id == identity.principal_id)
    rows = q.all()
    return [
        {
            "id": c.id,
            "email": c.principal.email if c.principal else None,
            "name": (
                _contact_name(session, c.principal.email)
                or (c.principal.name if c.principal else None)
            ),
            "role": c.role,
            "note": c.note,
            "active": c.active,
            "created_at": _iso(c.created_at),
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
def revoke_credential(
    credential_id: int,
    body: dict | None = None,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    reason = (body or {}).get("reason") or "管理员吊销"
    return credentials.revoke_credential(session, credential_id, actor=identity.email, reason=reason)


@router.post("/credentials/{credential_id}/restore")
def restore_credential(
    credential_id: int,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    return credentials.restore_credential(session, credential_id, actor=identity.email)


@router.delete("/credentials/{credential_id}")
def delete_credential(
    credential_id: int,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    try:
        return credentials.delete_credential(session, credential_id, actor=identity.email)
    except catalog.DomainError as e:
        raise HTTPException(status_code=409, detail=e.message)


@router.post("/credentials/renew")
def renew_credential(
    credential_id: int | None = None,
    days: int = 90,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
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
    from app import runtime
    return {
        "status": "ok",
        "version": get_version(),
        "last_patrol_at": _iso(last.ended_at) if last else None,
        "started_at": _iso(runtime.STARTED_AT),
        "uptime_seconds": round(runtime.uptime_seconds(), 1),
        "uptime_human": runtime.format_uptime(),
    }


# ---------- 排队（Web「排队」页） ----------

@router.get("/queue")
def queue_snapshot(
    identity: web_auth.WebIdentity | None = Depends(web_auth.optional_identity),
    session: Session = Depends(get_session),
):
    """当前队列：按机型组 + 按设备 + 全量条目（队位 / priority / offer 倒计时）。"""
    now = utcnow()
    q = session.query(QueueEntry).order_by(QueueEntry.priority.desc(), QueueEntry.joined_at.asc())
    if _is_user_view(identity):
        q = q.filter(QueueEntry.principal_id == identity.principal_id)
    entries = q.all()
    # v0.12：展开行需要「是谁 / 等哪台机型 / 等多久」，一次性取好映射避免 N+1。
    cred_labels = {c.id: c.agent_label for c in session.query(Credential).all()}
    device_model = {d.device_id: d.model_class for d in session.query(Device).all()}

    def _waited_text(seconds: int) -> str:
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days} 天")
        if hours:
            parts.append(f"{hours} 小时")
        if mins:
            parts.append(f"{mins} 分")
        parts.append(f"{secs} 秒")
        return "".join(parts)

    items = []
    for e in entries:
        remaining = None
        if e.offer_expires_at:
            remaining = int((e.offer_expires_at - now).total_seconds())
        email = e.principal.email if e.principal else None
        waited_seconds = max(0, int((now - e.joined_at).total_seconds())) if e.joined_at else 0
        items.append(
            {
                "entry_id": e.entry_id,
                "target_type": e.target_type,
                "target_code": e.target_code,
                "model": e.target_code if e.target_type == "product" else device_model.get(e.target_code),
                "email": email,
                "name": _contact_name(session, email) or (e.principal.name if e.principal else None),
                "agent_label": cred_labels.get(e.credential_id),
                "purpose": e.purpose,
                "priority": e.priority,
                "state": e.state,
                "position": queue_position(session, e) if e.state == QueueState.QUEUED else None,
                "waited_seconds": waited_seconds,
                "waited_text": _waited_text(waited_seconds),
                "joined_at": _iso(e.joined_at),
                "offered_at": _iso(e.offered_at),
                "offer_expires_at": _iso(e.offer_expires_at),
                "offer_remaining_seconds": remaining,
                "lease_id": e.lease_id,
            }
        )

    def _group(target_type: str) -> list[dict]:
        buckets: dict[str, dict] = {}
        for it in items:
            if it["target_type"] != target_type:
                continue
            g = buckets.setdefault(
                it["target_code"],
                {"target_code": it["target_code"], "queued": 0, "offered": 0, "waiters": []},
            )
            if it["state"] == QueueState.QUEUED:
                g["queued"] += 1
            elif it["state"] == QueueState.OFFERED:
                g["offered"] += 1
            g["waiters"].append(it)
        return list(buckets.values())

    return {
        "entries": items,
        "by_model": _group("product"),
        "by_device": _group("device"),
    }


# ---------- 异常 / 待修复（Web「异常/待修复」页） ----------

def _anomaly_device_row(d: Device, now) -> dict:
    return {
        "device_id": d.device_id,
        "display_name": d.display_name,
        "model_class": d.model_class,
        "owner": d.owner,
        "position_code": d.position_code,
        "health_level": d.health_level,
        "consecutive_failures": d.consecutive_failures,
        "last_seen_at": _iso(d.last_seen_at),
        "created_at": _iso(d.created_at),
        "days_pending": (now - d.created_at).days if d.created_at else None,
    }


@router.get("/anomalies")
def anomalies(session: Session = Depends(get_session)):
    """异常台：offline 列表（含连续失败轮次）、suspect、pending 超 30 天待清理。"""
    now = utcnow()
    cutoff = now - timedelta(days=PENDING_CLEANUP_DAYS)
    offline = (
        session.query(Device).filter(Device.status == DeviceStatus.OFFLINE)
        .order_by(Device.device_id).all()
    )
    suspect = (
        session.query(Device)
        .filter(Device.status == DeviceStatus.AVAILABLE, Device.health_level != HealthLevel.L3)
        .order_by(Device.device_id).all()
    )
    cleanup = (
        session.query(Device)
        .filter(Device.status == DeviceStatus.PENDING, Device.created_at <= cutoff)
        .order_by(Device.created_at).all()
    )
    return {
        "offline": [_anomaly_device_row(d, now) for d in offline],
        "suspect": [_anomaly_device_row(d, now) for d in suspect],
        "cleanup": [_anomaly_device_row(d, now) for d in cleanup],
        "pending_cleanup_days": PENDING_CLEANUP_DAYS,
    }


# ---------- 审计（Web「审计」页） ----------

@router.get("/audit")
def audit(
    action: str | None = None,
    limit: int = 200,
    identity: web_auth.WebIdentity | None = Depends(web_auth.optional_identity),
    session: Session = Depends(get_session),
):
    """审计流水：时间 / 主体 / 动作 / 对象，可按动作类型筛选。"""
    q = session.query(AuditEvent).order_by(AuditEvent.created_at.desc())
    if _is_user_view(identity):
        q = q.filter(AuditEvent.actor == identity.email)
    if action:
        q = q.filter(AuditEvent.action == action)
    rows = q.limit(limit).all()
    events = [
        {
            "event_id": e.event_id,
            "actor": e.actor,
            "action": e.action,
            "device_id": e.device_id,
            "lease_id": e.lease_id,
            "object": e.device_id or e.lease_id,
            "detail": e.detail,
            "created_at": _iso(e.created_at),
        }
        for e in rows
    ]
    actions = sorted({a for (a,) in session.query(AuditEvent.action).distinct().all()})
    return {"events": events, "actions": actions}


# ---------- 设置（Web「设置」页） ----------

@router.get("/settings")
def settings_view(session: Session = Depends(get_session)):
    """系统配置 + 机型字典（供维护）。位置规则为只读展示。"""
    view = {
        "product_types": catalog.list_products(session),
        "location_rule": {
            "format": "推荐：302-R2-05（楼栋-机架-机位）或 工位/会议室 描述，如「A区工位-12」「3F会议室-01」",
            "description": "位置为描述性文本，建议规范表达但非强制；注册时自动去空白，位置全局唯一。可用 res_list_positions 查询全部设备位置与 IP。",
            "mandatory": False,
        },
        "probe_backend": settings.probe_backend,
    }
    view.update(runtime_public_view(session))
    return view


@router.put("/settings")
def update_settings(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """在线修改运行时配置（白名单键，持久化到 settings KV，立即生效）。"""
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Body 须为 JSON 对象")
    unknown = [k for k in payload.keys() if k not in RUNTIME_KEYS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的配置键：{', '.join(sorted(unknown))}；仅接受 {', '.join(sorted(RUNTIME_KEYS))}",
        )
    if not payload:
        raise HTTPException(status_code=400, detail="未提供任何可更新字段")
    errors: dict[str, str] = {}
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        nv, err = validate_runtime_value(key, value)
        if err:
            errors[key] = err
        else:
            normalized[key] = nv
    if errors:
        raise HTTPException(status_code=400, detail={"message": "配置校验失败", "fields": errors})
    for key, value in normalized.items():
        set_runtime_value(session, key, value)
    session.commit()
    return settings_view(session=session)


@router.get("/product-types")
def list_product_types(session: Session = Depends(get_session)):
    """设备类型完整列表（含 6 个元数据字段 + 完整度标签），供设置页「设备类型」Tab。"""
    return catalog.list_products(session)


@router.put("/product-types/{type_code}")
def update_product_type(
    type_code: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """admin 编辑机型工程/编译元数据（6 字段）。允许留空（信息不全）。"""
    pt = session.get(ProductType, type_code)
    if pt is None:
        raise HTTPException(status_code=404, detail=f"机型 {type_code} 不存在")
    for field in ("defconfig", "sdk_repo_cmd", "build_cmd", "image_path", "jenkins_url", "notes"):
        if field in payload:
            value = payload[field]
            setattr(pt, field, (str(value).strip() if value is not None else None) or None)
    # v0.13：支持「停用 / 重新启用」同一入口（bool 或 "true"/"false" 字符串都兼容）。
    if "enabled" in payload:
        value = payload["enabled"]
        pt.enabled = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
    session.commit()
    return {
        "type_code": type_code,
        "complete": catalog.product_complete(pt),
        "complete_label": "完整" if catalog.product_complete(pt) else "信息不全",
        "enabled": pt.enabled,
    }


@router.post("/product-types")
def add_product_type(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    code = (payload.get("type_code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="type_code 必填")
    if session.get(ProductType, code) is not None:
        raise HTTPException(status_code=409, detail=f"机型 {code} 已存在")
    pt = ProductType(
        type_code=code,
        display_name=payload.get("display_name") or code,
        has_screen=bool(payload.get("has_screen")),
        has_led=bool(payload.get("has_led")),
        serial_count=int(payload.get("serial_count") or 0),
        adb_port=int(payload.get("adb_port") or 5555),
        enabled=True,
        model_pattern=payload.get("model_pattern"),
        created_at=utcnow(),
    )
    session.add(pt)
    session.commit()
    return {"type_code": code, "display_name": pt.display_name, "enabled": True}


@router.post("/product-types/{type_code}/disable")
def disable_product_type(
    type_code: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    pt = session.get(ProductType, type_code)
    if pt is None:
        raise HTTPException(status_code=404, detail=f"机型 {type_code} 不存在")
    pt.enabled = False
    session.commit()
    return {"type_code": type_code, "enabled": False}


@router.delete("/product-types/{type_code}")
def delete_product_type(
    type_code: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """物理删除机型；存在设备或活动租约/排队引用时返回 409。"""
    pt = session.get(ProductType, type_code)
    if pt is None:
        raise HTTPException(status_code=404, detail=f"机型 {type_code} 不存在")

    devices = session.query(Device).filter(Device.model_class == type_code).all()
    device_count = len(devices)
    device_ids = [d.device_id for d in devices]
    lease_count = 0
    if device_ids:
        lease_count = (
            session.query(func.count(Lease.lease_id))
            .filter(Lease.device_id.in_(device_ids), Lease.state.in_(LeaseState.ACTIVE))
            .scalar()
            or 0
        )
    active_queue_states = (QueueState.QUEUED, QueueState.OFFERED)
    product_queue = (
        session.query(func.count(QueueEntry.entry_id))
        .filter(
            QueueEntry.target_type == "product",
            QueueEntry.target_code == type_code,
            QueueEntry.state.in_(active_queue_states),
        )
        .scalar()
        or 0
    )
    device_queue = 0
    if device_ids:
        device_queue = (
            session.query(func.count(QueueEntry.entry_id))
            .filter(
                QueueEntry.target_type == "device",
                QueueEntry.target_code.in_(device_ids),
                QueueEntry.state.in_(active_queue_states),
            )
            .scalar()
            or 0
        )
    queue_count = product_queue + device_queue

    if device_count or lease_count or queue_count:
        raise HTTPException(
            status_code=409,
            detail=(
                f"机型 {type_code} 仍有引用：设备 {device_count}、"
                f"活动租约 {lease_count}、活动排队 {queue_count}；"
                f"请先迁移/停用设备并释放租约/取消排队后再删除"
            ),
        )
    session.delete(pt)
    session.commit()
    return {"type_code": type_code, "deleted": True}


# ---------- 人员通讯录（v0.11，管理员） ----------

def _contact_row(c: Contact) -> dict:
    return {
        "email": c.email,
        "name": c.name,
        "note": c.note,
        "created_at": _iso(c.created_at),
    }


@router.get("/contacts")
def list_contacts(
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    rows = session.query(Contact).order_by(Contact.email).all()
    return [_contact_row(c) for c in rows]


@router.post("/contacts")
def add_contact(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    email = str(payload.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="邮箱不合法")
    if session.get(Contact, email) is not None:
        raise HTTPException(status_code=409, detail="联系人已存在")
    c = Contact(
        email=email,
        name=str(payload.get("name") or "").strip() or None,
        note=str(payload.get("note") or "").strip() or None,
        created_at=utcnow(),
    )
    session.add(c)
    session.commit()
    return _contact_row(c)


@router.put("/contacts/{email}")
def update_contact(
    email: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    c = session.get(Contact, email)
    if c is None:
        raise HTTPException(status_code=404, detail="联系人不存在")
    if "name" in payload:
        c.name = str(payload["name"]).strip() or None
    if "note" in payload:
        c.note = str(payload["note"]).strip() or None
    session.commit()
    return _contact_row(c)


@router.delete("/contacts/{email}")
def delete_contact(
    email: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    c = session.get(Contact, email)
    if c is None:
        raise HTTPException(status_code=404, detail="联系人不存在")
    session.delete(c)
    session.commit()
    return {"email": email, "deleted": True}


# ---------- SMTP 配置与测试邮件（v0.11，管理员） ----------

@router.get("/settings/smtp")
def get_smtp_config(
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    return mailer.get_smtp_config(session)


@router.put("/settings/smtp")
def put_smtp_config(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    return mailer.save_smtp_config(session, payload)


@router.post("/settings/smtp/test")
def smtp_test(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    to = str(payload.get("to") or "").strip()
    if not to or "@" not in to:
        raise HTTPException(status_code=400, detail="收件人邮箱不合法")
    try:
        mailer.send_mail(
            session,
            to,
            "【Quamm TRES】SMTP 测试邮件",
            "这是一封 Quamm TRES 的 SMTP 配置测试邮件，收到即表示配置正确。",
        )
    except Exception as e:  # noqa: BLE001 - 网络/认证错误统一透给前端
        raise HTTPException(status_code=502, detail=f"发送失败：{e}")
    return {"sent": True, "to": to}


# ---------- 发接入凭据（v0.13：先生成预览、再确认发信；旧 send 保留兼容） ----------

_CREDENTIAL_BATCH_TTL = timedelta(minutes=30)
# token 明文只在生成时返回一次，之后库里只有 hash。确认发信前把明文暂存在内存里，
# 30 分钟有效；确认发送（或批次过期）后立即清除。单进程 uvicorn 足够，重启即失效。
_pending_batches: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _store_pending_batch(batch: dict) -> None:
    """明文批次入内存，顺带清掉已过期批次。"""
    now = utcnow()
    with _pending_lock:
        for key in [k for k, v in _pending_batches.items() if v["expires_at"] <= now]:
            _pending_batches.pop(key, None)
        _pending_batches[batch["batch_id"]] = batch


def _take_pending_batch(batch_id: str) -> dict | None:
    """取回并移除一个未过期批次；不存在 / 已过期返回 None。"""
    with _pending_lock:
        batch = _pending_batches.pop(batch_id, None)
        if batch is None:
            return None
        if batch["expires_at"] <= utcnow():
            return None
        return batch


def _collect_recipients(payload: dict) -> list[str]:
    """通讯录多选 + 手填邮箱（逗号/分号/换行分隔），去重保序，仅保留含 @ 的地址。"""
    emails = [str(e).strip().lower() for e in (payload.get("emails") or []) if str(e).strip()]
    manual = str(payload.get("manual_emails") or "")
    for part in manual.replace(",", "\n").replace(";", "\n").splitlines():
        if part.strip():
            emails.append(part.strip().lower())
    seen: set[str] = set()
    ordered: list[str] = []
    for email in emails:
        if "@" in email and email not in seen:
            seen.add(email)
            ordered.append(email)
    return ordered


def _create_credentials(
    session: Session, ordered: list[str], role: str, note: str | None, valid_days: int
) -> list[tuple[str, str, str, bool]]:
    """为每人建 principal/contact + 一条 rr_ 凭据（库里只存 hash）。

    返回 (display_email, name, token, has_email)。display_email 为对外展示邮箱。
    """
    now = utcnow()
    quota = int(get_runtime_value(session, "holder_concurrency_limit"))
    created: list[tuple[str, str, str, bool]] = []
    for email in ordered:
        contact = session.get(Contact, email)
        name = (contact.name if contact and contact.name else None) or email
        principal = session.query(Principal).filter(Principal.email == email).first()
        if principal is None:
            principal = Principal(
                email=email,
                name=name,
                employee_no=None,
                quota=quota,
                is_admin=False,
                status="active",
                created_at=now,
            )
            session.add(principal)
            session.flush()
        if contact is None:
            session.add(Contact(email=email, name=name, note=note, created_at=now))
        token = new_token()
        session.add(
            Credential(
                principal_id=principal.id,
                token_hash=hash_token(token),
                token_prefix=token_prefix(token),
                expires_at=now + timedelta(days=valid_days),
                role=role,
                note=note,
                created_at=now,
            )
        )
        created.append((email, name, token, True))
    return created


def _create_manual_holder_credentials(
    session: Session,
    holders: list[dict],
    role: str,
    batch_note: str | None,
    valid_days: int,
) -> list[tuple[str, str, str, bool]]:
    """无邮箱条目：用 manual-<短id>@local 占位建 Principal，返回中不暴露占位邮箱。"""
    now = utcnow()
    quota = int(get_runtime_value(session, "holder_concurrency_limit"))
    created: list[tuple[str, str, str, bool]] = []
    for item in holders:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        note = str(item.get("note") or "").strip() or batch_note
        placeholder = f"manual-{new_id('', 4)}@local"
        principal = session.query(Principal).filter(Principal.email == placeholder).first()
        if principal is None:
            principal = Principal(
                email=placeholder,
                name=name,
                employee_no=None,
                quota=quota,
                is_admin=False,
                status="active",
                created_at=now,
            )
            session.add(principal)
            session.flush()
        else:
            principal.name = name
        token = new_token()
        session.add(
            Credential(
                principal_id=principal.id,
                token_hash=hash_token(token),
                token_prefix=token_prefix(token),
                expires_at=now + timedelta(days=valid_days),
                role=role,
                note=note,
                created_at=now,
            )
        )
        # 对外不暴露占位 key：email 置空，仅展示姓名
        created.append(("", name, token, False))
    return created


def _collect_manual_holders(payload: dict) -> list[dict]:
    raw = payload.get("manual_holders") or []
    if not isinstance(raw, list):
        return []
    holders: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        note = str(item.get("note") or "").strip() or None
        holders.append({"name": name, "note": note})
    return holders


def _valid_days(payload: dict, session: Session) -> int:
    default_days = int(get_runtime_value(session, "credential_valid_days"))
    try:
        days = int(payload.get("valid_days") or default_days)
    except (TypeError, ValueError):
        days = default_days
    return max(1, min(days, 3650))


@router.post("/credentials/batch-generate")
def batch_generate_credentials(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """只生成 token 入库 + 渲染邮件主题/正文（真实 token），明文暂存 30 分钟；不发信。

    返回 token 明文（仅此一次）、渲染好的邮件主题/正文、含首个 token 的 mcpServers 配置。
    支持 emails / manual_emails，以及无邮箱的 manual_holders（仅姓名）。
    """
    ordered = _collect_recipients(payload)
    holders = _collect_manual_holders(payload)
    if not ordered and not holders:
        raise HTTPException(status_code=400, detail="请至少选择联系人或填写邮箱，或添加仅姓名条目")
    role = payload.get("role") if payload.get("role") in ("admin", "user") else "user"
    note = str(payload.get("note") or "").strip() or None
    valid_days = _valid_days(payload, session)

    created = _create_credentials(session, ordered, role, note, valid_days)
    created.extend(_create_manual_holder_credentials(session, holders, role, note, valid_days))
    if not created:
        raise HTTPException(status_code=400, detail="请至少选择联系人或填写邮箱，或添加仅姓名条目")
    session.commit()

    results = [
        {
            "email": email or None,
            "name": name,
            "role": role,
            "token": token,
            "has_email": has_email,
            "subject": mailer.CREDENTIAL_SUBJECT,
            "body": mailer.build_credential_body(token, note),
        }
        for email, name, token, has_email in created
    ]
    batch_id = new_id("batch_", 6)
    expires_at = utcnow() + _CREDENTIAL_BATCH_TTL
    _store_pending_batch(
        {"batch_id": batch_id, "results": results, "note": note, "expires_at": expires_at}
    )
    return {
        "batch_id": batch_id,
        "count": len(results),
        "expires_at": _iso(expires_at),
        "valid_days": valid_days,
        "role": role,
        "results": results,
        "mcp_config": mailer.build_mcp_config(results[0]["token"]),
        "mcp_config_text": mailer.mcp_config_text(results[0]["token"]),
    }


@router.post("/credentials/batch-send")
def batch_send_credentials(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """按 batch_id 取回 30 分钟内生成的明文，逐人真正 SMTP 发信；发完立即清除 pending。

    无邮箱（has_email=False）条目跳过发信，token 仍有效。
    """
    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        raise HTTPException(status_code=400, detail="缺少 batch_id")
    batch = _take_pending_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=410, detail="批次不存在或已超过 30 分钟，请重新生成凭据")

    cfg = mailer.get_smtp_config(session)
    results = []
    for item in batch["results"]:
        r = {
            "email": item.get("email"),
            "name": item.get("name"),
            "token": item["token"],
            "role": item["role"],
            "sent": False,
            "skipped": False,
            "error": None,
        }
        if not item.get("has_email", True) or not item.get("email"):
            r["skipped"] = True
            results.append(r)
            continue
        try:
            mailer.send_mail(session, item["email"], item["subject"], item["body"], cfg=cfg)
            r["sent"] = True
        except Exception as e:  # noqa: BLE001 - 单人失败不影响其他人，token 仍可用
            r["error"] = str(e)
        results.append(r)
    return {"count": len(results), "results": results}


# 兼容：原来「一按即生成 + 直接发信」的用法保留，Web 不再调用。
@router.post("/credentials/send")
def send_credentials(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """为每人生成一条 rr_ token（带 role/note），逐人调 SMTP 发送 MCP 配置邮件。"""
    emails = [str(e).strip().lower() for e in (payload.get("emails") or []) if str(e).strip()]
    manual = str(payload.get("manual_emails") or "")
    for part in manual.replace(",", "\n").replace(";", "\n").splitlines():
        if part.strip():
            emails.append(part.strip().lower())

    seen: set[str] = set()
    ordered: list[str] = []
    for email in emails:
        if "@" in email and email not in seen:
            seen.add(email)
            ordered.append(email)
    if not ordered:
        raise HTTPException(status_code=400, detail="请至少选择联系人或填写邮箱")

    role = payload.get("role") if payload.get("role") in ("admin", "user") else "user"
    note = str(payload.get("note") or "").strip() or None
    now = utcnow()
    created: list[tuple[str, str]] = []
    for email in ordered:
        contact = session.get(Contact, email)
        name = (contact.name if contact and contact.name else None) or email
        principal = session.query(Principal).filter(Principal.email == email).first()
        if principal is None:
            principal = Principal(
                email=email,
                name=name,
                employee_no=None,
                quota=int(get_runtime_value(session, "holder_concurrency_limit")),
                is_admin=False,
                status="active",
                created_at=now,
            )
            session.add(principal)
            session.flush()
        if contact is None:
            session.add(Contact(email=email, name=name, note=note, created_at=now))
        token = new_token()
        session.add(
            Credential(
                principal_id=principal.id,
                token_hash=hash_token(token),
                token_prefix=token_prefix(token),
                expires_at=now + timedelta(days=int(get_runtime_value(session, "credential_valid_days"))),
                role=role,
                note=note,
                created_at=now,
            )
        )
        created.append((email, token))
    session.commit()

    cfg = mailer.get_smtp_config(session)
    results = []
    for email, token in created:
        item = {"email": email, "token": token, "role": role, "sent": False, "error": None}
        try:
            mailer.send_mail(
                session,
                email,
                mailer.CREDENTIAL_SUBJECT,
                mailer.build_credential_body(token, note),
                cfg=cfg,
            )
            item["sent"] = True
        except Exception as e:  # noqa: BLE001 - 单人失败不影响其他人，token 仍可用
            item["error"] = str(e)
        results.append(item)
    return {"count": len(results), "results": results}
