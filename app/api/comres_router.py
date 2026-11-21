"""COMRes 北向路由（契约 §2.7）。

节点侧 register / unregister / heartbeat：Bearer node_token。
占用方 allocated-heartbeat、资源视图 resources：Web 登录（管理面）。
旧 /api/wirelog/* 保持不变。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.db import get_session
from app.domain import comres
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
        status = 401 if "UNAUTHORIZED" in e.code or "RETIRED" in e.code else (
            404 if "NOT_FOUND" in e.code else 400)
        raise HTTPException(status_code=status, detail={
            "code": e.code, "message": e.message, "detail": e.detail})


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
