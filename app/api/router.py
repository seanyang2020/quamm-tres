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
from fastapi.responses import FileResponse
from pathlib import Path
from sqlalchemy import func
from sqlalchemy.orm import Session

from app import mailer, screenshot, web_auth
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
from app.domain import catalog, credentials, feedback, leasing, patrol
from app import discovery
from app.domain.queueing import position as queue_position
from app.domain import build_servers as buildsvc
from app.domain.catalog import DomainError
from app import sdk_build
from app.models import (
    AuditEvent,
    Contact,
    Credential,
    Device,
    FlashJob,
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
    return {"email": auth.principal.email, "name": name, "role": role,
            "principal_id": auth.principal.id}


@router.get("/auth/me")
def auth_me(identity: web_auth.WebIdentity = Depends(web_auth.require_login)):
    return {"email": identity.email, "name": identity.name, "role": identity.role,
            "principal_id": identity.principal_id}


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


@router.put("/devices/{device_id}")
def web_device_update(
    device_id: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    """编辑设备：管理员可改全部；注册者可改基础信息与共享策略。"""
    try:
        return catalog.device_update(
            session,
            device_id=device_id,
            actor_email=identity.email,
            is_admin=(identity.role == "admin"),
            changes=payload,
        )
    except catalog.DomainError as e:
        raise _domain_http(e)


@router.delete("/devices/{device_id}")
def delete_device_route(
    device_id: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    try:
        return catalog.device_unregister(
            session,
            device_id,
            actor_email=identity.email,
            is_admin=(identity.role == "admin"),
        )
    except DomainError as e:
        raise _domain_http(e)


@router.post("/devices/probe")
def web_device_probe(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """Web 手工添加设备第一步：按 IP/端口探测，返回 form_id 与探测结果。"""
    ip = str(payload.get("ip") or "").strip()
    if not ip:
        raise HTTPException(status_code=400, detail="ip 必填")
    port = int(payload.get("port") or 5555)
    try:
        return catalog.device_probe(
            session,
            ip=ip,
            port=port,
            hint_model=(str(payload.get("hint_model") or "").strip() or None),
            hint_position=(str(payload.get("hint_position") or "").strip() or None),
            source=(str(payload.get("source") or "").strip() or "real"),
            agent_info="web-console",
        )
    except catalog.DomainError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.post("/devices/{device_id}/probe")
def web_device_probe_registered(
    device_id: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """v0.28：手动探测一台已注册设备（设备页「探测」按钮）。"""
    try:
        return catalog.device_probe_registered(
            session, device_id=device_id,
            actor_email=identity.email, is_admin=(identity.role == "admin"),
        )
    except catalog.DomainError as e:
        raise _domain_http(e)


@router.post("/devices/probe-batch")
def web_device_probe_batch(
    payload: dict | None = Body(None),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """v0.28：批量探测待检/离线设备（单次最多 20 台）。"""
    body = payload or {}
    statuses = body.get("statuses")
    device_ids = body.get("device_ids")
    if isinstance(statuses, str):
        statuses = [statuses]
    if isinstance(device_ids, str):
        device_ids = [device_ids]
    try:
        return catalog.device_probe_batch(
            session,
            statuses=[str(s) for s in statuses] if statuses else None,
            device_ids=[str(x) for x in device_ids] if device_ids else None,
            actor_email=identity.email,
            limit=int(body.get("limit") or 20),
        )
    except catalog.DomainError as e:
        raise _domain_http(e)


@router.post("/devices/register")
def web_device_register(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """Web 手工添加设备第二步：确认位置/负责人/机型后入库。"""
    form_id = str(payload.get("form_id") or "").strip()
    if not form_id:
        raise HTTPException(status_code=400, detail="form_id 必填")
    try:
        return catalog.device_register(
            session,
            form_id=form_id,
            position_code=str(payload.get("position_code") or "").strip(),
            owner=str(payload.get("owner") or "").strip(),
            actor_email=identity.email,
            model_class=(str(payload.get("model_class") or "").strip() or None),
            note=(str(payload.get("note") or "").strip() or None),
            agent_info="web-console",
            share_scope=(str(payload.get("share_scope") or "").strip() or None),
            allowed_emails=payload.get("allowed_emails") or None,
            allowed_agent_labels=payload.get("allowed_agent_labels") or None,
        )
    except catalog.DomainError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.post("/devices/discover")
def web_device_discover(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """设备发现模式：扫描网段（可指定 networks，缺省自动探测本机 /24），返回可注册设备。"""
    raw = payload.get("networks")
    if isinstance(raw, str):
        networks = [raw] if raw.strip() else None
    elif isinstance(raw, list):
        networks = [str(x) for x in raw if str(x).strip()]
    else:
        networks = None
    try:
        return discovery.scan(
            session,
            networks=networks,
            port=int(payload.get("port") or 5555),
            timeout=float(payload.get("timeout") or 1.0),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------- 设备截图（v0.23，管理员） ----------

@router.post("/devices/{device_id}/screenshot")
def device_screenshot(
    device_id: str,
    payload: dict | None = Body(None),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """管理员对设备截图并保存到服务器；返回文件路径与元数据（cookie 鉴权）。"""
    body = payload or {}
    try:
        quality = int(body.get("quality") or 60)
        rotate = int(body.get("rotate") or 0)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="quality/rotate 须为整数")
    try:
        return screenshot.capture_screenshot(
            device_id=device_id, quality=quality, rotate=rotate,
            session=session, created_by=identity.email,
        )
    except catalog.DomainError as e:
        raise HTTPException(status_code=400, detail=e.message)


@router.get("/screenshots/{file_name}")
def get_screenshot(
    file_name: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
):
    """读取已生成的截图（FileResponse，按文件名严格校验，防路径穿越）。"""
    # 只接受 <device>_<UTC时间戳>.jpg：白名单正则 + basename 一致 + 目录包含校验三层。
    if (
        not screenshot._SAFE_FILENAME_RE.fullmatch(file_name)
        or Path(file_name).name != file_name
    ):
        raise HTTPException(status_code=400, detail="非法文件名")
    base = screenshot.SCREENSHOT_DIR.resolve()
    target = (base / file_name).resolve()
    if target.parent != base or not target.is_file():
        raise HTTPException(status_code=404, detail="截图不存在")
    return FileResponse(str(target), media_type="image/jpeg")


# ---------- 截图记录管理（v0.24，管理员） ----------

@router.get("/screenshot-records")
def list_screenshot_records(
    device_id: str | None = None,
    limit: int = 200,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """列出截图历史（最新在前），可按 device_id 过滤。"""
    return {
        "items": screenshot.list_screenshots(
            session, device_id=device_id, limit=limit
        )
    }


@router.get("/screenshot-records/stats")
def screenshot_stats(
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """截图存储统计：总条数 / 占用空间(bytes) / 涉及设备数。"""
    from app import screenshot_lifecycle

    return screenshot_lifecycle.stats(session)


@router.delete("/screenshot-records/{shot_id}")
def remove_screenshot_record(
    shot_id: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """删除单张截图（记录 + 文件）。"""
    if not screenshot.delete_screenshot(session, shot_id=shot_id, actor=identity.email):
        raise HTTPException(status_code=404, detail="截图记录不存在")
    return {"shot_id": shot_id, "deleted": True}


@router.post("/screenshot-records/prune")
def prune_screenshot_records(
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """管理员「立即清理」：执行全局 prune（保留期 + 总数上限）。"""
    from app import screenshot_lifecycle

    return screenshot_lifecycle.prune_global(session, actor=identity.email)


# ---------- 缓存回收（v0.28.3） ----------


@router.get("/maintenance/cache")
def cache_usage(
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
):
    """查看 DB 源缓存 / 刷机缓存占用与当前保留策略。"""
    from app import cache_prune

    return cache_prune.cache_stats()


@router.post("/maintenance/cache/prune")
def prune_cache(
    payload: dict | None = Body(default=None),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
):
    """立即回收缓存。body {"dry_run": true} 只统计不删。"""
    from app import cache_prune

    dry_run = bool((payload or {}).get("dry_run"))
    result = cache_prune.prune_all(dry_run=dry_run)
    return {**result, "actor": identity.email}


@router.get("/feedback")
def list_feedback(
    status: str | None = None,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    return feedback.list_feedback(session, status=status)


@router.post("/feedback/{feedback_id}/review")
def review_feedback(
    feedback_id: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    try:
        return feedback.review_feedback(
            session,
            feedback_id,
            status=str(payload.get("status") or ""),
            admin_note=payload.get("admin_note"),
        )
    except catalog.DomainError as e:
        raise HTTPException(status_code=400, detail=e.message)


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
def list_credentials(
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
    force: bool = False,
    session: Session = Depends(get_session),
):
    try:
        if force:
            return credentials.force_delete_credential(session, credential_id, actor=identity.email)
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
    # v0.28：探测后端（mock/real）在线切换后立即重建全局 prober
    if "probe_backend" in normalized:
        from app.domain.probe import setup_prober_from_settings

        setup_prober_from_settings(session)
    return settings_view(session=session)


def _product_flag(value, default: bool = False) -> bool:
    """兼容 bool 与 "1/true/yes/on" 字符串。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _product_int(value, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < minimum:
        return minimum
    if maximum is not None and n > maximum:
        return maximum
    return n


def _apply_product_eng_fields(pt: ProductType, payload: dict) -> None:
    """工程/编译/能力元数据：6 个自由文本 + 芯片型号/浮点 ABI/FPU。"""
    for field in ("defconfig", "sdk_repo_cmd", "build_cmd", "image_path", "jenkins_url", "notes"):
        if field in payload:
            value = payload[field]
            setattr(pt, field, (str(value).strip() if value is not None else None) or None)
    if "chip_model" in payload:
        value = payload["chip_model"]
        pt.chip_model = (str(value).strip() if value is not None else None) or None
    if "float_abi" in payload:
        value = str(payload["float_abi"] or "auto").strip().lower()
        pt.float_abi = value if value in ("hard", "soft", "auto") else "auto"
    if "has_fpu" in payload:
        pt.has_fpu = _product_flag(payload["has_fpu"])


def _apply_product_basic_fields(pt: ProductType, payload: dict) -> None:
    """v0.28.7：展示名、基础属性、固件策略、编译服务器引用 —— 编辑时也都能改。

    type_code 是机器主键（API/排队/设备归属都靠它），display_name 是给人看的名字，
    改名不影响任何引用，所以这里允许直接改 display_name。
    """
    if "display_name" in payload:
        pt.display_name = (str(payload.get("display_name") or "").strip() or pt.type_code)[:128]
    if "has_screen" in payload:
        pt.has_screen = _product_flag(payload["has_screen"])
    if "has_led" in payload:
        pt.has_led = _product_flag(payload["has_led"])
    if "serial_count" in payload:
        pt.serial_count = _product_int(payload["serial_count"], pt.serial_count, 0, 64)
    if "adb_port" in payload:
        pt.adb_port = _product_int(payload["adb_port"], pt.adb_port, 1, 65535)
    if "model_pattern" in payload:
        value = payload["model_pattern"]
        pt.model_pattern = (str(value).strip() if value is not None else None) or None
    if "db_project_name" in payload:
        value = payload["db_project_name"]
        pt.db_project_name = (str(value).strip() if value is not None else None) or None
    for field in ("required_major", "required_minor", "required_patch"):
        if field in payload:
            setattr(pt, field, _product_int(payload[field], getattr(pt, field), 0, 9999))
    if "build_server_id" in payload:
        value = payload["build_server_id"]
        pt.build_server_id = (str(value).strip() if value is not None else None) or None


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
    _apply_product_eng_fields(pt, payload)
    _apply_product_basic_fields(pt, payload)
    # v0.13：支持「停用 / 重新启用」同一入口（bool 或 "true"/"false" 字符串都兼容）。
    if "enabled" in payload:
        value = payload["enabled"]
        pt.enabled = value if isinstance(value, bool) else str(value).strip().lower() in ("1", "true", "yes", "on")
    # v0.28.5：支持同时修改主键 type_code，并级联迁移已有设备/历史刷机记录。
    new_code = payload.get("new_type_code")
    new_code = (str(new_code).strip() if new_code is not None else "") or ""
    previous_code = type_code
    moved_devices = 0
    moved_flash_jobs = 0
    moved_queue_entries = 0
    # 迁移前先算好返回值，避免删除旧行后再访问 ORM 对象触发重新加载。
    complete_now = catalog.product_complete(pt)
    enabled_now = pt.enabled
    if new_code and new_code != type_code:
        if len(new_code) > 64:
            raise HTTPException(status_code=400, detail="new_type_code 过长（最多 64 字符）")
        if not all(c.isalnum() or c in "_.-" for c in new_code):
            raise HTTPException(status_code=400, detail="new_type_code 只能包含字母、数字、下划线、点、连字符")
        if session.get(ProductType, new_code) is not None:
            raise HTTPException(status_code=409, detail=f"机型 {new_code} 已存在，请先处理同名机型")
        session.flush()  # 先把普通字段落到旧行，克隆时读到的是最新值
        data = {c.name: getattr(pt, c.name) for c in ProductType.__table__.columns}
        data["type_code"] = new_code
        # 顺序：先插新行 -> 再迁子表引用 -> 最后删旧行，任何时刻外键都成立。
        session.add(ProductType(**data))
        session.flush()
        moved_devices = (
            session.query(Device)
            .filter(Device.model_class == previous_code)
            .update({"model_class": new_code}, synchronize_session=False)
        )
        moved_flash_jobs = (
            session.query(FlashJob)
            .filter(FlashJob.product_type == previous_code)
            .update({"product_type": new_code}, synchronize_session=False)
        )
        # 排队记录里 target_type='product' 的 target_code 就是机型，必须一起迁，
        # 否则排队项会指向一个不存在的机型，排队的人永远等不到设备。
        moved_queue_entries = (
            session.query(QueueEntry)
            .filter(QueueEntry.target_type == "product", QueueEntry.target_code == previous_code)
            .update({"target_code": new_code}, synchronize_session=False)
        )
        session.expunge(pt)
        session.query(ProductType).filter(ProductType.type_code == previous_code).delete(
            synchronize_session=False
        )
    session.commit()
    return {
        "type_code": new_code or previous_code,
        "previous_type_code": previous_code,
        "renamed": bool(new_code and new_code != previous_code),
        "moved_devices": moved_devices,
        "moved_flash_jobs": moved_flash_jobs,
        "moved_queue_entries": moved_queue_entries,
        "complete": complete_now,
        "complete_label": "完整" if complete_now else "信息不全",
        "enabled": enabled_now,
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
    # v0.28.7：派生 —— 以现有机型为模板克隆一行（工程配置/芯片浮点/编译服务器全部带过来），
    # 只换 type_code 与展示名；不搬运任何设备，设备仍留在模板机型上。
    copy_from = str(payload.get("copy_from") or "").strip()
    if copy_from:
        tpl = session.get(ProductType, copy_from)
        if tpl is None:
            raise HTTPException(status_code=404, detail=f"派生模板机型 {copy_from} 不存在")
        data = {c.name: getattr(tpl, c.name) for c in ProductType.__table__.columns}
        data["type_code"] = code
        data["enabled"] = True
        data["display_name"] = (
            (str(payload.get("display_name") or "").strip()) or f"{tpl.display_name}-副本"
        )[:128]
        data["created_at"] = utcnow()
        derived = ProductType(**data)
        session.add(derived)
        session.flush()
        _apply_product_eng_fields(derived, payload)
        _apply_product_basic_fields(derived, payload)
        session.commit()
        return {
            "type_code": code,
            "display_name": derived.display_name,
            "enabled": True,
            "derived_from": copy_from,
        }
    fa = str(payload.get("float_abi") or "auto").strip().lower()
    fa = fa if fa in ("hard", "soft", "auto") else "auto"
    hf = payload.get("has_fpu")
    has_fpu = hf if isinstance(hf, bool) else str(hf or "").strip().lower() in ("1", "true", "yes", "on")
    chip_model = payload.get("chip_model")
    pt = ProductType(
        type_code=code,
        display_name=payload.get("display_name") or code,
        has_screen=bool(payload.get("has_screen")),
        has_led=bool(payload.get("has_led")),
        serial_count=int(payload.get("serial_count") or 0),
        adb_port=int(payload.get("adb_port") or 5555),
        enabled=True,
        model_pattern=payload.get("model_pattern"),
        chip_model=(str(chip_model).strip() or None) if chip_model is not None else None,
        float_abi=fa,
        has_fpu=has_fpu,
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
# 已发送结果的重放窗口：响应丢失时客户端可在该窗口内安全重放，不会重复发信
_SENT_REPLAY_TTL = timedelta(minutes=10)
# token 明文只在生成时返回一次，之后库里只有 hash。确认发信前把明文暂存在内存里，
# 30 分钟有效；确认发送（或批次过期）后立即清除。单进程 uvicorn 足够，重启即失效。
_pending_batches: dict[str, dict] = {}
_sent_batches: dict[str, dict] = {}
# 客户端幂等键 -> batch_id，网络重试同一逻辑请求时复用同一批次，避免重复建凭据
_idempotency_keys: dict[str, dict] = {}
_pending_lock = threading.Lock()


def _store_pending_batch(batch: dict, response: dict) -> None:
    """明文批次入内存，顺带清掉已过期批次/幂等键。"""
    now = utcnow()
    with _pending_lock:
        for key in [k for k, v in _pending_batches.items() if v["expires_at"] <= now]:
            _pending_batches.pop(key, None)
        for key in [k for k, v in _idempotency_keys.items() if v["expires_at"] <= now]:
            _idempotency_keys.pop(key, None)
        for key in [k for k, v in _sent_batches.items() if v["expires_at"] <= now]:
            _sent_batches.pop(key, None)
        batch["response"] = response
        _pending_batches[batch["batch_id"]] = batch


def bind_idempotency_key(key: str, batch_id: str) -> None:
    with _pending_lock:
        _idempotency_keys[key] = {"batch_id": batch_id, "expires_at": utcnow() + _CREDENTIAL_BATCH_TTL}


def replay_idempotent_batch(key: str) -> dict | None:
    """幂等键命中且批次仍有效时，返回其原始响应。"""
    with _pending_lock:
        rec = _idempotency_keys.get(key)
        if rec is None or rec["expires_at"] <= utcnow():
            return None
        batch = _pending_batches.get(rec["batch_id"])
        if batch is None or batch["expires_at"] <= utcnow():
            return None
        return batch.get("response")


def _take_pending_batch(batch_id: str) -> dict | None:
    """取回（不移除）一个未过期批次；不存在 / 已过期返回 None。"""
    with _pending_lock:
        batch = _pending_batches.get(batch_id)
        if batch is None:
            return None
        if batch["expires_at"] <= utcnow():
            return None
        return batch


def get_sent_replay(batch_id: str) -> dict | None:
    with _pending_lock:
        rec = _sent_batches.get(batch_id)
        if rec is None or rec["expires_at"] <= utcnow():
            return None
        return rec["result"]


def store_sent_result(batch_id: str, result: dict) -> None:
    now = utcnow()
    with _pending_lock:
        _pending_batches.pop(batch_id, None)
        _sent_batches[batch_id] = {"result": result, "expires_at": now + _SENT_REPLAY_TTL}


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
    支持 idempotency_key：同一键的网络重试在 30 分钟内复用同一批次，不重复建凭据。
    """
    idem_key = str(payload.get("idempotency_key") or "").strip()
    if idem_key:
        replay = replay_idempotent_batch(idem_key)
        if replay is not None:
            return replay
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
    response = {
        "batch_id": batch_id,
        "count": len(results),
        "expires_at": _iso(expires_at),
        "valid_days": valid_days,
        "role": role,
        "results": results,
        "mcp_config": mailer.build_mcp_config(results[0]["token"]),
        "mcp_config_text": mailer.mcp_config_text(results[0]["token"]),
    }
    _store_pending_batch(
        {"batch_id": batch_id, "results": results, "note": note, "expires_at": expires_at},
        response,
    )
    if idem_key:
        bind_idempotency_key(idem_key, batch_id)
    return response


@router.post("/credentials/batch-send")
def batch_send_credentials(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """按 batch_id 取回 30 分钟内生成的明文，逐人真正 SMTP 发信。

    无邮箱（has_email=False）条目跳过发信，token 仍有效。
    发送结果保留 10 分钟：响应丢失时同一 batch_id 重放返回原结果，不会重复发信。
    """
    batch_id = str(payload.get("batch_id") or "").strip()
    if not batch_id:
        raise HTTPException(status_code=400, detail="缺少 batch_id")
    replay = get_sent_replay(batch_id)
    if replay is not None:
        return replay
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
    out = {"count": len(results), "results": results}
    store_sent_result(batch_id, out)
    return out


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

# ---- v0.25：编译服务器（账号级私有，一账号一台，自助配置 + 自动探测）----
def _domain_http(exc: DomainError) -> HTTPException:
    code = getattr(exc, "code", "")
    if code in ("FORBIDDEN", "ACCESS_DENIED"):
        status = 403
    elif code == "NOT_FOUND":
        status = 404
    elif code in (
        "BUILD_SERVER_NAME_TAKEN",
        "BUILD_SERVER_IN_USE",
        "BUILD_SERVER_ALREADY_EXISTS",
        "HAS_ACTIVE_LEASE",
    ):
        status = 409
    else:
        status = 400
    return HTTPException(
        status_code=status,
        detail={"code": code, "message": exc.message, "detail": exc.detail},
    )


@router.get("/build-servers")
def list_build_servers(
    include_all: bool = False,
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    # v0.27 默认按账号隔离；管理员排障时显式 ?all=true 才看全部
    return buildsvc.list_build_servers(
        session,
        principal_id=identity.principal_id,
        is_admin=(identity.role == "admin"),
        include_all=include_all,
    )


@router.post("/build-servers")
def create_build_server_route(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    try:
        return buildsvc.create_build_server(
            session,
            actor=identity.email,
            principal_id=identity.principal_id,
            name=payload.get("name", ""),
            host=payload.get("host", ""),
            port=int(payload.get("port") or 22),
            username=payload.get("username", ""),
            auth_mode=payload.get("auth_mode", "password"),
            secret=payload.get("secret") or None,
            work_root=payload.get("work_root") or "tres-auto",
        )
    except DomainError as e:
        raise _domain_http(e)


@router.put("/build-servers/{server_id}")
def update_build_server_route(
    server_id: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    try:
        return buildsvc.update_build_server(
            session,
            actor=identity.email,
            principal_id=identity.principal_id,
            is_admin=(identity.role == "admin"),
            server_id=server_id,
            name=payload.get("name"),
            host=payload.get("host"),
            port=payload.get("port"),
            username=payload.get("username"),
            auth_mode=payload.get("auth_mode"),
            secret=payload.get("secret") or None,
            work_root=payload.get("work_root"),
        )
    except DomainError as e:
        raise _domain_http(e)


@router.delete("/build-servers/{server_id}")
def delete_build_server_route(
    server_id: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    try:
        return buildsvc.delete_build_server(
            session,
            actor=identity.email,
            principal_id=identity.principal_id,
            is_admin=(identity.role == "admin"),
            server_id=server_id,
        )
    except DomainError as e:
        raise _domain_http(e)


@router.post("/build-servers/{server_id}/probe")
def probe_build_server_route(
    server_id: str,
    payload: dict = Body(default={}),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    try:
        return buildsvc.probe_build_server(
            session,
            principal_id=identity.principal_id,
            is_admin=(identity.role == "admin"),
            server_id=server_id,
            subdir=(payload or {}).get("subdir"),
        )
    except DomainError as e:
        raise _domain_http(e)


@router.post("/sdk-build")
def sdk_build_route(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    try:
        return sdk_build.run_sdk_build(
            session,
            principal_id=identity.principal_id,
            type_code=payload.get("type_code", ""),
            ip_suffix=payload.get("ip_suffix") or None,
            timeout=int(payload.get("timeout") or 1800),
        )
    except DomainError as e:
        raise _domain_http(e)


# ---------------- v0.29 设备层串口（tres-logs raw TCP） ----------------
from app.domain import serial as serial_svc


def _require_serial_lease(session, identity, device_id: str, action_key=None, confirm=False):
    serial_svc.check_access(
        session,
        device_id,
        email=identity.email,
        is_admin=(identity.role == "admin"),
        action_key=action_key,
        confirm=confirm,
    )


@router.get("/devices/{device_id}/serial")
def get_device_serial(device_id: str, session: Session = Depends(get_session)):
    """串口能力声明：端口、动作（含 steps）、电源/模式状态、当前占用。"""
    try:
        return serial_svc.caps(session, device_id)
    except DomainError as e:
        raise _domain_http(e)


@router.put("/devices/{device_id}/serial")
def put_device_serial(
    device_id: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """配置端口与动作（仅管理员）。ports 元素结构见规划文档 §3。"""
    try:
        return serial_svc.upsert_ports(
            session,
            device_id,
            payload.get("ports") or [],
            actor=identity.email,
            replace=bool(payload.get("replace")),
        )
    except DomainError as e:
        raise _domain_http(e)


@router.delete("/devices/{device_id}/serial/{slot}")
def delete_device_serial(
    device_id: str,
    slot: int,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    try:
        return serial_svc.delete_port(session, device_id, slot, actor=identity.email)
    except DomainError as e:
        raise _domain_http(e)


@router.post("/devices/{device_id}/serial/action")
def post_serial_action(
    device_id: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    """服务端执行一个已定义动作（前端「测试」按钮也走这里）。"""
    action_key = str(payload.get("action_key") or "").strip()
    if not action_key:
        raise HTTPException(status_code=400, detail="action_key 必填")
    try:
        _require_serial_lease(session, identity, device_id, action_key, bool(payload.get("confirm")))
        return serial_svc.run_action(
            session,
            device_id,
            action_key,
            by=f"web:{identity.email}",
            lease_id=payload.get("lease_id"),
            hold_ms=payload.get("hold_ms"),
            wait_ms=payload.get("wait_ms"),
            dry_run=bool(payload.get("dry_run")),
        )
    except DomainError as e:
        raise _domain_http(e)


@router.post("/devices/{device_id}/serial/read")
def post_serial_read(
    device_id: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    """短读串口（默认 log 口）。"""
    try:
        _require_serial_lease(session, identity, device_id)
        return serial_svc.read_serial(
            session,
            device_id,
            slot=payload.get("slot"),
            seconds=float(payload.get("seconds") or 3.0),
            by=f"web:{identity.email}",
        )
    except DomainError as e:
        raise _domain_http(e)


@router.post("/devices/{device_id}/serial/probe")
def post_serial_probe(
    device_id: str,
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    """自检串口：能否连上、有没有回包。"""
    try:
        _require_serial_lease(session, identity, device_id)
        return serial_svc.probe_port(
            session, device_id, slot=payload.get("slot"), by=f"web:{identity.email}"
        )
    except DomainError as e:
        raise _domain_http(e)


@router.post("/devices/{device_id}/serial/state/reset")
def post_serial_state_reset(
    device_id: str,
    identity: web_auth.WebIdentity = Depends(web_auth.require_admin),
    session: Session = Depends(get_session),
):
    """把串口状态归零（换电源板后用）。"""
    try:
        return serial_svc.reset_state(session, device_id, actor=identity.email)
    except DomainError as e:
        raise _domain_http(e)


@router.post("/serial/report")
def post_serial_report(
    payload: dict = Body(...),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    """skill 直连 TCP 执行后回填状态与审计。"""
    device_id = str(payload.get("device_id") or "").strip()
    action_key = str(payload.get("action_key") or "").strip()
    if not device_id or not action_key:
        raise HTTPException(status_code=400, detail="device_id / action_key 必填")
    try:
        return serial_svc.report_result(
            session,
            device_id,
            action_key,
            str(payload.get("result") or ""),
            by=str(payload.get("by") or f"web:{identity.email}"),
            detail=payload.get("detail"),
        )
    except DomainError as e:
        raise _domain_http(e)


@router.post("/devices/{device_id}/serial/release")
def post_serial_release(
    device_id: str,
    payload: dict = Body(default={}),
    identity: web_auth.WebIdentity = Depends(web_auth.require_login),
    session: Session = Depends(get_session),
):
    """手动释放该设备占用的物理串口（共享串口控制完必须放，异常中断后用它兜底）。"""
    try:
        _require_serial_lease(session, identity, device_id)
        return serial_svc.release_device_ports(
            session, device_id, actor=f"web:{identity.email}", slot=payload.get("slot")
        )
    except DomainError as e:
        raise _domain_http(e)
