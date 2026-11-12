"""WireLog 北向路由（见 WIRELOG-DESIGN.md API-1..10）。

节点侧（register/heartbeat/report）用 Bearer node_token 鉴权；
管理侧用 TRES Web 会话（require_login / require_admin）。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import socket as _socket

from fastapi import (APIRouter, Body, Depends, Header, HTTPException, Response,
                     WebSocket)
from sqlalchemy.orm import Session

from app.db import SessionLocal, get_session
from app.domain import wirelog
from app.domain.catalog import DomainError
from app.models import Credential, Principal, WireLogNode, WireLogPortBinding
from app.utils import utcnow
from app import web_auth

router = APIRouter(prefix="/wirelog")


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Bearer token")
    return authorization.split(" ", 1)[1].strip()


def _domain_http(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DomainError as e:
        status = 401 if "UNAUTHORIZED" in e.code else (
            404 if "NOT_FOUND" in e.code else 400)
        raise HTTPException(status_code=status, detail={
            "code": e.code, "message": e.message, "detail": e.detail})


# ---------------- 节点侧（API-1..3，响应带 HMAC 签名头）

def _signed(session, token, payload, data):
    nonce = str(payload.get("nonce") or secrets.token_hex(8))
    try:
        body, headers = wirelog.sign_response(session, token, nonce, data)
    except DomainError as e:
        status = 401 if "UNAUTHORIZED" in e.code else 400
        raise HTTPException(status_code=status, detail={
            "code": e.code, "message": e.message})
    return Response(content=body, media_type="application/json", headers=headers)


@router.post("/register")
def register(authorization: str | None = Header(default=None),
             payload: dict = Body(...),
             session: Session = Depends(get_session)):
    token = _bearer(authorization)
    data = _domain_http(wirelog.register, session, token, payload)
    return _signed(session, token, payload, data)


@router.post("/heartbeat")
def heartbeat(authorization: str | None = Header(default=None),
              payload: dict = Body(...),
              session: Session = Depends(get_session)):
    token = _bearer(authorization)
    data = _domain_http(wirelog.heartbeat, session, token, payload)
    return _signed(session, token, payload, data)


@router.post("/report")
def report(authorization: str | None = Header(default=None),
           payload: dict = Body(...),
           session: Session = Depends(get_session)):
    token = _bearer(authorization)
    data = _domain_http(wirelog.report_event, session, token, payload)
    return _signed(session, token, payload, data)


# ---------------- 管理侧（API-4..10）

@router.get("/nodes")
def nodes(session: Session = Depends(get_session),
          _id=Depends(web_auth.require_login)):
    return wirelog.list_nodes(session)


@router.get("/nodes/{node_id}")
def node_detail(node_id: str, session: Session = Depends(get_session),
                _id=Depends(web_auth.require_login)):
    return _domain_http(wirelog.get_node, session, node_id)


@router.get("/device/{device_id}/bindings")
def device_wl_bindings(device_id: str, session: Session = Depends(get_session),
                       _id=Depends(web_auth.require_login)):
    return wirelog.device_bindings(session, device_id)


@router.put("/nodes/{node_id}/config")
def put_config(node_id: str, payload: dict = Body(...),
               session: Session = Depends(get_session),
               identity=Depends(web_auth.require_admin)):
    return _domain_http(wirelog.set_config, session, node_id,
                        payload, identity.email)


@router.post("/nodes/{node_id}/bind")
def post_bind(node_id: str, payload: dict = Body(default={}),
              session: Session = Depends(get_session),
              identity=Depends(web_auth.require_login)):
    return _domain_http(wirelog.bind_port, session, node_id,
                        payload.get("slot"), payload.get("device_id"),
                        identity.email)


@router.post("/nodes/{node_id}/write-grant")
def post_grant(node_id: str, payload: dict = Body(...),
               session: Session = Depends(get_session),
               identity=Depends(web_auth.require_admin)):
    if payload.get("revoke"):
        return _domain_http(wirelog.revoke_write, session, node_id,
                            payload["slot"], identity.email)
    return _domain_http(wirelog.grant_write, session, node_id,
                        payload["slot"], payload["principal"], identity.email)


@router.post("/enroll")
def post_enroll(payload: dict = Body(default={}),
                session: Session = Depends(get_session),
                identity=Depends(web_auth.require_admin)):
    return wirelog.enroll(
        session, adb_address=payload.get("adb_address"),
        operator=identity.email, display_name=payload.get("display_name"),
        model_class=payload.get("model_class", "esl2_hk"))


@router.post("/nodes/{node_id}/uart-register")
def post_uart_register(node_id: str, payload: dict = Body(default={}),
                       session: Session = Depends(get_session),
                       identity=Depends(web_auth.require_login)):
    return _domain_http(wirelog.uart_register, session, node_id,
                        payload.get("slot"), identity.email,
                        payload.get("position_code"), payload.get("owner"))


@router.post("/nodes/{node_id}/retire")
def post_retire(node_id: str, session: Session = Depends(get_session),
                identity=Depends(web_auth.require_admin)):
    return _domain_http(wirelog.retire_node, session, node_id, identity.email)


@router.delete("/nodes/{node_id}")
def delete_node(node_id: str, session: Session = Depends(get_session),
                identity=Depends(web_auth.require_admin)):
    return _domain_http(wirelog.delete_node, session, node_id, identity.email)

# ---------------- 浏览器串口 shell（WebSocket ↔ serialhub raw TCP，v0.29.3）

SHELL_CONNECT_TIMEOUT_S = 5.0
SHELL_LOGIN_TIMEOUT_S = 6.0
SHELL_MAX_BUFFER_HINT = 256 * 1024


def _caps_dict(node) -> dict:
    caps = node.caps_json
    if isinstance(caps, dict):
        return caps
    if isinstance(caps, str) and caps.strip().startswith("{"):
        try:
            return json.loads(caps)
        except ValueError:
            return {}
    return {}


def _ws_identity(session, ws):
    """WebSocket 里的登录态校验（cookie 与 HTTP 同源，逻辑对齐 optional_identity）。"""
    data = web_auth.read_session(ws.cookies.get(web_auth.SESSION_COOKIE))
    if data is None:
        return None
    cred = session.get(Credential, data.get("cid"))
    if cred is None or cred.revoked:
        return None
    if cred.expires_at is not None and cred.expires_at <= utcnow():
        return None
    principal = session.get(Principal, cred.principal_id)
    if principal is None or principal.status != "active":
        return None
    role = "admin" if (cred.role == "admin" or principal.is_admin) else "user"
    return {"email": principal.email, "role": role, "credential_id": cred.id}


@router.websocket("/nodes/{node_id}/ports/{slot}/shell")
async def node_port_shell(ws: WebSocket, node_id: str, slot: int):
    """把浏览器接到日志机某一路串口的 raw TCP 交互口（简易版 serial-bridge lsb）。

    协议（serialhub 1.0.0 实测）：TCP 连上 -> 出现 ``login: `` ->
    发 ``<local_token>\\n`` -> 回 ``OK`` -> 发 ``WRITE\\n`` -> 回 ``WRITE OK``
    拿到写锁后，后续字节直通被串联口。行结束必须是 LF（CR 不认）。

    前端消息（JSON）：上行 ``{"type":"data","data":"..."}``；下行
    ``{"type":"out","data":"..."}`` / ``{"type":"status",...}`` / ``{"type":"error",...}``。
    """
    await ws.accept()

    def send(obj):
        return ws.send_text(json.dumps(obj, ensure_ascii=False))

    sock = None
    session = SessionLocal()
    try:
        ident = _ws_identity(session, ws)
        if ident is None:
            await send({"type": "error", "message": "未登录或会话已失效，请重新登录"})
            await ws.close(code=4401)
            return
        node = session.get(WireLogNode, node_id)
        if node is None or not node.ip:
            await send({"type": "error", "message": "日志机不存在或未上报 IP"})
            await ws.close(code=4404)
            return
        binding = (session.query(WireLogPortBinding)
                   .filter_by(node_id=node_id, slot=int(slot)).one_or_none())
        caps = _caps_dict(node)
        base_port = int(caps.get("raw_tcp_port") or 9001)
        port = base_port + (int(slot) - 1)          # 单路时即 base_port，多路按序推导
        token = str(caps.get("local_token") or "")
        host = node.ip
        tty = binding.tty_name if binding is not None else None
        baud = binding.baud if binding is not None else None
        granted = (binding.write_granted_to or "").strip() if binding is not None else ""
        can_write = ident["role"] == "admin" or granted == ident["email"]
        node_name = node.display_name or node.node_id
    finally:
        session.close()

    loop = asyncio.get_running_loop()
    endpoint = f"tcp://{host}:{port}"
    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.setblocking(False)
    try:
        await asyncio.wait_for(loop.sock_connect(sock, (host, port)),
                               timeout=SHELL_CONNECT_TIMEOUT_S)
    except Exception as exc:                     # noqa: BLE001 - 统一报给前端
        sock.close()
        await send({"type": "error",
                    "message": f"连不上串口服务器 {endpoint}：{exc}"})
        await ws.close(code=1011)
        return

    await send({"type": "status", "state": "tcp", "endpoint": endpoint,
                "node": node_name, "slot": int(slot), "tty": tty, "baud": baud,
                "can_write": bool(can_write)})

    async def read_until(needle: str, timeout: float) -> str:
        buf = b""
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            remain = max(0.05, deadline - loop.time())
            try:
                chunk = await asyncio.wait_for(loop.sock_recv(sock, 4096), timeout=remain)
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buf += chunk
            if needle and needle in buf.decode("utf-8", "replace"):
                break
        return buf.decode("utf-8", "replace")

    try:
        banner = await read_until("login:", 3.0)
        if banner:
            await send({"type": "out", "data": banner})
        if token:
            await loop.sock_sendall(sock, (token + "\n").encode())
            resp = await read_until("OK", SHELL_LOGIN_TIMEOUT_S)
            if resp:
                await send({"type": "out", "data": resp})
            if "OK" not in resp:
                await send({"type": "status", "state": "login-failed",
                            "message": "serialhub 登录失败（local_token 可能已轮换）"})
        if can_write:
            await loop.sock_sendall(sock, b"WRITE\n")
            resp = await read_until("WRITE OK", SHELL_LOGIN_TIMEOUT_S)
            if resp:
                await send({"type": "out", "data": resp})
            writable = "WRITE OK" in resp
            await send({"type": "status",
                        "state": "ready" if writable else "readonly",
                        "can_write": writable,
                        "message": "" if writable else "写锁未拿到（可能被其它会话占用），当前只读"})
        else:
            await send({"type": "status", "state": "ready", "can_write": False,
                        "message": "当前账号无写授权，只读查看输出"})
    except Exception as exc:                     # noqa: BLE001
        sock.close()
        await send({"type": "error", "message": f"串口登录阶段失败：{exc}"})
        await ws.close(code=1011)
        return

    async def pump_out():
        while True:
            chunk = await loop.sock_recv(sock, 4096)
            if not chunk:
                await send({"type": "status", "state": "closed",
                            "message": "串口连接已关闭"})
                return
            await send({"type": "out", "data": chunk.decode("utf-8", "replace")})

    async def pump_in():
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except ValueError:
                msg = {"type": "data", "data": raw}
            kind = msg.get("type")
            if kind == "data":
                payload = str(msg.get("data") or "")
                if payload:
                    await loop.sock_sendall(sock, payload.encode())
            elif kind == "ping":
                await send({"type": "pong"})

    tasks = [asyncio.create_task(pump_out()), asyncio.create_task(pump_in())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            if t not in done:
                t.cancel()
    except Exception:                            # noqa: BLE001
        pass
    finally:
        for t in tasks:
            t.cancel()
        try:
            sock.close()
        except OSError:
            pass
        try:
            await ws.close()
        except Exception:                        # noqa: BLE001
            pass


