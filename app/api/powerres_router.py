"""PowerRes 北向路由：电源资源 inventory / bind / sync。

管理面：web_auth.require_login。
节点面 sync：Bearer node_token（与 comres_router 一致）。
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app import web_auth
from app.db import get_session
from app.domain import powerres, wirelog
from app.domain.catalog import DomainError

router = APIRouter(prefix="/powerres", tags=["powerres"])


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    return authorization.split(" ", 1)[1].strip()


def _domain_http(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DomainError as e:
        code = e.code or ""
        if "UNAUTHORIZED" in code or "RETIRED" in code:
            status = 401
        elif "NOT_FOUND" in code or code == "POWERRES_UNKNOWN_CHANNEL":
            status = 404
        elif "BUSY" in code:
            status = 409
        else:
            status = 400
        raise HTTPException(status_code=status, detail={
            "code": e.code, "message": e.message, "detail": e.detail,
        })


@router.get("/overview")
def overview(
    session: Session = Depends(get_session),
    _id=Depends(web_auth.require_login),
):
    """只含有 power 资源或已有绑定的节点，供前端节点下拉。"""
    return _domain_http(powerres.overview, session)


@router.get("/inventory")
def inventory(
    node_id: str = Query(...),
    session: Session = Depends(get_session),
    _id=Depends(web_auth.require_login),
):
    return _domain_http(powerres.list_inventory, session, node_id)


@router.post("/inventory")
def inventory_upsert(
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    _id=Depends(web_auth.require_admin),
):
    """手工登记电源资源（无 hub 上报时的兜底）；幂等 upsert。"""
    return _domain_http(
        powerres.upsert_inventory_item,
        session,
        payload.get("node_id"),
        payload.get("power_dev"),
        payload.get("channel"),
        name=payload.get("name"),
        com=payload.get("com"),
        endpoint=payload.get("endpoint"),
        state=payload.get("state"),
        location=payload.get("location"),
        driver=payload.get("driver"),
    )


@router.delete("/inventory")
def inventory_delete(
    node_id: str = Query(...),
    power_dev: str = Query(...),
    channel: int = Query(...),
    session: Session = Depends(get_session),
    _id=Depends(web_auth.require_admin),
):
    return _domain_http(
        powerres.delete_inventory_item,
        session,
        node_id,
        power_dev,
        channel,
    )


@router.get("/bindings")
def bindings(
    node_id: str = Query(...),
    session: Session = Depends(get_session),
    _id=Depends(web_auth.require_login),
):
    return _domain_http(powerres.list_bindings, session, node_id)


@router.post("/bind")
def bind(
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    _id=Depends(web_auth.require_admin),
):
    return _domain_http(
        powerres.bind,
        session,
        payload.get("node_id"),
        payload.get("power_dev"),
        payload.get("channel"),
        payload.get("endpoint"),
        device_id=payload.get("device_id"),
        label=payload.get("label"),
        com=payload.get("com"),
        slot=payload.get("slot"),
    )


@router.post("/unbind")
def unbind(
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    _id=Depends(web_auth.require_admin),
):
    return _domain_http(
        powerres.unbind,
        session,
        payload.get("node_id"),
        payload.get("power_dev"),
        payload.get("channel"),
    )


@router.post("/action")
def channel_action(
    payload: dict = Body(...),
    session: Session = Depends(get_session),
    identity=Depends(web_auth.require_admin),
):
    """PT7a：管理员通道级开/关/重启/查询（不经被测设备 DeviceSerialPort）。"""
    return _domain_http(
        powerres.channel_action,
        session,
        payload.get("node_id"),
        payload.get("power_dev"),
        payload.get("channel"),
        payload.get("action"),
        by=f"web:{identity.email}",
        hold_ms=payload.get("hold_ms"),
    )


@router.post("/sync")
def sync(
    authorization: str | None = Header(default=None),
    payload: dict = Body(...),
    session: Session = Depends(get_session),
):
    """节点上报电源 inventory；node_id 缺省用 token 对应节点。"""
    token = _bearer(authorization)

    def _run():
        node = wirelog._auth_node(session, token)
        node_id = (payload.get("node_id") or "").strip() or node.node_id
        if node_id != node.node_id:
            raise DomainError(
                "POWERRES_NODE_MISMATCH",
                f"payload.node_id={node_id} 与 token 节点 {node.node_id} 不一致",
            )
        devices = payload.get("devices") or []
        return powerres.sync_inventory(session, node_id, devices)

    return _domain_http(_run)
