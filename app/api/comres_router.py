"""COMRes 北向路由（契约 §2.7）。

节点侧 register / unregister / heartbeat：Bearer node_token。
占用方 allocated-heartbeat、资源视图 resources：Web 登录（管理面）。
客户端自助申请 apply / 轮询：无需登录（限流）；审批与设置：登录 cookie。
旧 /api/wirelog/* 保持不变。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.db import get_session
from app.domain import comres, comres_apply
from app.domain.catalog import DomainError
from app import web_auth

router = APIRouter(prefix="/comres")


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    return authorization.split(" ", 1)[1].strip()


def _domain_http(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DomainError as e:
        if e.code == "COMRES_RATE_LIMITED":
            raise HTTPException(status_code=429, detail={
                "code": e.code, "message": e.message, "detail": e.detail})
        status = 401 if "UNAUTHORIZED" in e.code or "RETIRED" in e.code else (
            404 if "NOT_FOUND" in e.code else 400)
        raise HTTPException(status_code=status, detail={
            "code": e.code, "message": e.message, "detail": e.detail})


def _client_ip(request: Request) -> str | None:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    if request.client:
        return request.client.host
    return None


@router.post("/register")
def register(authorization: str | None = Header(default=None),
             payload: dict = Body(...),
             session: Session = Depends(get_session)):
    token = _bearer(authorization)
    return _domain_http(comres.register, session, token, payload)


@router.post("/unregister")
def unregister(authorization: str | None = Header(default=None),
               payload: dict = Body(...),
               session: Session = Depends(get_session)):
    token = _bearer(authorization)
    return _domain_http(comres.unregister, session, token, payload)


@router.post("/heartbeat")
def heartbeat(authorization: str | None = Header(default=None),
              payload: dict = Body(...),
              session: Session = Depends(get_session)):
    token = _bearer(authorization)
    return _domain_http(comres.heartbeat, session, token, payload)


@router.post("/allocated-heartbeat")
def allocated_heartbeat(payload: dict = Body(...),
                        session: Session = Depends(get_session),
                        identity=Depends(web_auth.require_login)):
    return _domain_http(
        comres.allocated_heartbeat, session, payload,
        actor=getattr(identity, "email", None),
    )


@router.get("/resources")
def resources(session: Session = Depends(get_session),
              _id=Depends(web_auth.require_login)):
    return comres.list_resources(session)


# ---------------- 客户端自助申请（无需登录，限流） ----------------

@router.post("/apply")
def post_apply(request: Request,
               payload: dict = Body(...),
               session: Session = Depends(get_session)):
    ip = _client_ip(request)
    _domain_http(comres_apply.check_rate_limit, f"apply:{ip or 'unknown'}")
    return _domain_http(comres_apply.apply_client, session, payload, source_ip=ip)


@router.get("/apply/{client_id}")
def get_apply(client_id: str,
              request: Request,
              session: Session = Depends(get_session)):
    ip = _client_ip(request)
    _domain_http(comres_apply.check_rate_limit, f"apply-get:{ip or 'unknown'}")
    return _domain_http(comres_apply.get_apply_status, session, client_id)


# ---------------- 管理面：审批 / 设置 ----------------

@router.get("/applications")
def get_applications(status: str | None = Query(default=None),
                     page: int = Query(default=1, ge=1),
                     page_size: int = Query(default=50, ge=1, le=200),
                     session: Session = Depends(get_session),
                     _id=Depends(web_auth.require_login)):
    return comres_apply.list_applications(
        session, status=status, page=page, page_size=page_size,
    )


@router.post("/applications/{request_id}/approve")
def post_approve(request_id: int,
                 payload: dict = Body(default={}),
                 session: Session = Depends(get_session),
                 identity=Depends(web_auth.require_login)):
    return _domain_http(
        comres_apply.approve_application, session, request_id,
        actor=getattr(identity, "email", None),
        note=(payload or {}).get("note"),
    )


@router.post("/applications/{request_id}/reject")
def post_reject(request_id: int,
                payload: dict = Body(...),
                session: Session = Depends(get_session),
                identity=Depends(web_auth.require_login)):
    return _domain_http(
        comres_apply.reject_application, session, request_id,
        reason=(payload or {}).get("reason") or "",
        actor=getattr(identity, "email", None),
    )


@router.post("/applications/{request_id}/revoke")
def post_revoke(request_id: int,
                session: Session = Depends(get_session),
                identity=Depends(web_auth.require_login)):
    return _domain_http(
        comres_apply.revoke_application, session, request_id,
        actor=getattr(identity, "email", None),
    )


@router.get("/apply-settings")
def get_apply_settings(session: Session = Depends(get_session),
                       _id=Depends(web_auth.require_login)):
    return {"auto_approve": comres_apply.get_auto_approve(session)}


@router.put("/apply-settings")
def put_apply_settings(payload: dict = Body(...),
                       session: Session = Depends(get_session),
                       _id=Depends(web_auth.require_login)):
    if "auto_approve" not in (payload or {}):
        raise HTTPException(status_code=400, detail="需要 auto_approve")
    val = payload["auto_approve"]
    if isinstance(val, str):
        enabled = val.strip().lower() in ("1", "true", "yes", "on")
    else:
        enabled = bool(val)
    return {"auto_approve": comres_apply.set_auto_approve(session, enabled)}
