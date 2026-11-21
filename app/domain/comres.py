"""COMRes 域：TCP COM 资源注册 / 解注册 / 心跳 / 自动匹配 / 占用续约。

契约 §2.7。复用 wl_node / wl_port_binding / wl_audit；与旧 wirelog 北向并存。
唯一键：node_id + ip + tcp_port（落库为 com_key = "{ip}:{tcp_port}"）。
TRES 不主动连串口探活。
"""
from __future__ import annotations

import json
import re
from datetime import timedelta

from sqlalchemy.orm import Session

from app.domain.catalog import DomainError
from app.domain import wirelog
from app.models import Device, WireLogNode, WireLogPortBinding
from app.serial_tcp import SerialConfigError, parse_endpoint
from app.utils import utcnow

# 未分配资源：节点侧默认心跳间隔（契约 §2.7）
HEARTBEAT_INTERVAL_S = 3600
# 已分配资源：占用方续约缺省 TTL（契约 §2.7；Q4 待拍板，先按 15 分钟）
ALLOCATED_TTL_S = 15 * 60

_ENDPOINT_RE = re.compile(r"^tcp://.+", re.IGNORECASE)


def _audit(session, node_id, action, actor=None, result="ok", detail=None):
    wirelog._audit(session, node_id, action, actor=actor, result=result, detail=detail)


def _validate_endpoint(endpoint: str | None) -> str | None:
    """校验 endpoint：空串允许；非空必须是 tcp://host:port（不重写端口）。"""
    if endpoint is None:
        return None
    ep = str(endpoint).strip()
    if not ep:
        return ""
    if not _ENDPOINT_RE.match(ep):
        # 也接受裸 host:port，入库时统一补 tcp://（与 serial_tcp 口径一致）
        try:
            host, port = parse_endpoint(ep)
        except SerialConfigError as e:
            raise DomainError("COMRES_BAD_ENDPOINT", f"endpoint 格式非法: {ep}") from e
        return f"tcp://{host}:{port}"
    try:
        parse_endpoint(ep)
    except SerialConfigError as e:
        raise DomainError("COMRES_BAD_ENDPOINT", f"endpoint 格式非法: {ep}") from e
    return ep


def _parse_ip_port(endpoint: str | None, fallback_ip: str | None):
    """从 endpoint 解析 (ip, tcp_port)；endpoint 空时用 fallback_ip，port 未知。"""
    if endpoint:
        try:
            host, port = parse_endpoint(endpoint)
            return host, port
        except SerialConfigError:
            pass
    return (fallback_ip.strip() if fallback_ip else None), None


def make_com_key(ip: str | None, tcp_port: int | None) -> str | None:
    if not ip or tcp_port is None:
        return None
    return f"{ip}:{int(tcp_port)}"


def _is_allocated(binding: WireLogPortBinding, now=None) -> bool:
    """占用有效：有 allocated_by 且未过期；或仅有 bound_device_id 且 expire 未过期/未设。"""
    now = now or utcnow()
    if binding.allocated_expire is not None and binding.allocated_expire <= now:
        return False
    if binding.allocated_by:
        return True
    if binding.bound_device_id and binding.allocated_expire is not None:
        return True
    if binding.bound_device_id and binding.allocated_at is not None:
        return True
    return False


def _port_allocated_view(binding: WireLogPortBinding, now=None) -> dict:
    allocated = _is_allocated(binding, now)
    return {
        "com": binding.com_name,
        "slot": binding.slot,
        "endpoint": binding.endpoint,
        "allocated": allocated,
        "allocated_by": binding.allocated_by if allocated else None,
        "allocated_expire": (
            binding.allocated_expire.isoformat()
            if allocated and binding.allocated_expire else None
        ),
        "bound_device_id": binding.bound_device_id if allocated else None,
    }


def expire_allocations(session: Session) -> list[dict]:
    """超时未续约 → 释放回资源池并记审计。返回被释放的摘要列表。"""
    now = utcnow()
    rows = (session.query(WireLogPortBinding)
            .filter(WireLogPortBinding.allocated_expire.isnot(None))
            .filter(WireLogPortBinding.allocated_expire <= now)
            .all())
    released = []
    for b in rows:
        detail = {
            "com": b.com_name, "com_key": b.com_key, "endpoint": b.endpoint,
            "was_allocated_by": b.allocated_by,
            "was_device_id": b.bound_device_id,
            "allocated_expire": b.allocated_expire.isoformat() if b.allocated_expire else None,
        }
        b.allocated_by = None
        b.allocated_at = None
        b.allocated_expire = None
        b.bound_device_id = None
        _audit(session, b.node_id, "comres_release_expired",
               result="ok", detail=detail)
        released.append({"node_id": b.node_id, "com_key": b.com_key, **detail})
    if released:
        session.commit()
    return released


def _mark_allocated(binding: WireLogPortBinding, by: str, ttl_s: int | None = None):
    now = utcnow()
    ttl = int(ttl_s if ttl_s is not None else ALLOCATED_TTL_S)
    binding.allocated_by = by
    binding.allocated_at = now
    binding.allocated_expire = now + timedelta(seconds=ttl)


def _match_device(session: Session, port: dict, local_ip: str | None):
    """按 SN > MAC > IP 优先级匹配 device。返回 (device|None, basis)。"""
    sn = (port.get("sn") or "").strip()
    if sn:
        device = session.query(Device).filter(Device.sn == sn).one_or_none()
        if device is not None:
            return device, "sn"

    mac = (port.get("mac") or port.get("shell_mac") or "").strip().lower()
    if mac:
        device = session.query(Device).filter(Device.mac.ilike(mac)).one_or_none()
        if device is not None:
            return device, "mac"

    ip = (port.get("ip") or port.get("shell_ip") or local_ip or "").strip()
    # endpoint host 也可作 IP 匹配候选
    if not ip and port.get("endpoint"):
        try:
            ip, _ = parse_endpoint(str(port["endpoint"]))
        except SerialConfigError:
            ip = ""
    if ip:
        device = session.query(Device).filter(Device.ip == ip).one_or_none()
        if device is not None:
            return device, "ip"

    return None, "miss"


def _next_slot(session: Session, node_id: str) -> int:
    rows = (session.query(WireLogPortBinding.slot)
            .filter_by(node_id=node_id).all())
    used = {r[0] for r in rows}
    slot = 1
    while slot in used:
        slot += 1
    return slot


def _find_binding(session, node_id: str, *, com_key=None, slot=None,
                  com=None, endpoint=None, ip=None):
    q = session.query(WireLogPortBinding).filter_by(node_id=node_id)
    if com_key:
        return q.filter_by(com_key=com_key).one_or_none()
    if endpoint is not None or ip is not None:
        ep = _validate_endpoint(endpoint) if endpoint is not None else None
        host, port = _parse_ip_port(ep or None, ip)
        key = make_com_key(host, port)
        if key:
            hit = q.filter_by(com_key=key).one_or_none()
            if hit is not None:
                return hit
    if com:
        hit = q.filter(WireLogPortBinding.com_name == str(com).strip()).first()
        if hit is not None:
            return hit
    if slot is not None:
        return q.filter_by(slot=int(slot)).one_or_none()
    return None


def _upsert_comres_binding(session, node: WireLogNode, port: dict,
                           local_ip: str | None) -> WireLogPortBinding:
    """按 node_id+ip+tcp_port upsert；保留 slot 兼容旧唯一约束。"""
    raw_ep = port.get("endpoint")
    if "endpoint" in port:
        ep = _validate_endpoint(raw_ep)
        # 空串落库为 None（与 wirelog 口径一致）
        ep_store = ep if ep else None
    else:
        ep_store = None
        ep = None

    host, tcp_port = _parse_ip_port(ep_store, local_ip or node.ip)
    if "endpoint" in port and ep_store:
        host, tcp_port = parse_endpoint(ep_store)
    com_key = make_com_key(host, tcp_port)

    slot_raw = port.get("slot")
    binding = None
    if com_key:
        binding = (session.query(WireLogPortBinding)
                   .filter_by(node_id=node.node_id, com_key=com_key)
                   .one_or_none())
    if binding is None and slot_raw is not None:
        binding = (session.query(WireLogPortBinding)
                   .filter_by(node_id=node.node_id, slot=int(slot_raw))
                   .one_or_none())
    if binding is None:
        slot = int(slot_raw) if slot_raw is not None else _next_slot(session, node.node_id)
        binding = WireLogPortBinding(node_id=node.node_id, slot=slot)
        session.add(binding)

    if "endpoint" in port:
        binding.endpoint = ep_store
    if tcp_port is not None:
        binding.tcp_port = int(tcp_port)
    if com_key:
        binding.com_key = com_key

    com = port.get("com") or port.get("port_name") or port.get("com_name") or port.get("tty")
    if com:
        binding.com_name = str(com).strip()
    if port.get("tty"):
        binding.tty_name = str(port["tty"]).strip()
    elif com and not binding.tty_name:
        binding.tty_name = str(com).strip()
    if port.get("baud") is not None:
        baud = int(port["baud"])
        if baud <= 0:
            raise DomainError("COMRES_BAD_BAUD", f"baud 必须 > 0: {baud}")
        binding.baud = baud
    if port.get("health_state"):
        binding.health_state = str(port["health_state"])
    elif not binding.health_state or binding.health_state == "unknown":
        binding.health_state = "online"
    if port.get("probe") is not None:
        binding.probe_json = json.dumps(port["probe"], ensure_ascii=False)[:8000]
    if port.get("shell_mac") or port.get("mac"):
        binding.shell_mac = str(port.get("shell_mac") or port.get("mac")).strip().lower()
    if port.get("shell_ip") or port.get("ip"):
        binding.shell_ip = str(port.get("shell_ip") or port.get("ip")).strip()
    if "shell_reachable" in port:
        binding.shell_reachable = bool(port["shell_reachable"])
    binding.last_checked_at = utcnow()
    return binding


def _apply_auto_match(session, binding: WireLogPortBinding, port: dict,
                      local_ip: str | None) -> dict:
    """自动匹配并写审计；命中则绑定+标记 allocated。返回 match 摘要。"""
    # 已在有效占用中：不重复匹配抢占
    if _is_allocated(binding):
        return {
            "basis": "keep_allocated",
            "device_id": binding.bound_device_id,
            "allocated": True,
        }
    device, basis = _match_device(session, port, local_ip)
    if device is None:
        _audit(session, binding.node_id, "comres_match",
               result="miss", detail={
                   "com": binding.com_name, "com_key": binding.com_key,
                   "basis": "miss", "sn": port.get("sn"),
                   "mac": port.get("mac") or port.get("shell_mac"),
                   "ip": port.get("ip") or port.get("shell_ip") or local_ip,
               })
        return {"basis": "miss", "device_id": None, "allocated": False}

    binding.bound_device_id = device.device_id
    _mark_allocated(binding, by=f"auto:{basis}")
    _audit(session, binding.node_id, "comres_match",
           result="ok", detail={
               "com": binding.com_name, "com_key": binding.com_key,
               "basis": basis, "device_id": device.device_id,
               "allocated_by": binding.allocated_by,
               "allocated_expire": binding.allocated_expire.isoformat()
               if binding.allocated_expire else None,
           })
    return {"basis": basis, "device_id": device.device_id, "allocated": True}


def _auth_and_resolve_node(session, token: str, payload_node_id: str | None):
    node = wirelog._auth_node(session, token)
    if payload_node_id and payload_node_id != node.node_id:
        raise DomainError(
            "COMRES_NODE_MISMATCH",
            f"payload.node_id={payload_node_id} 与 token 节点 {node.node_id} 不一致",
        )
    return node


def _ports_response(bindings: list[WireLogPortBinding]) -> list[dict]:
    now = utcnow()
    out = []
    for b in bindings:
        view = _port_allocated_view(b, now)
        out.append({
            "com": view["com"],
            "slot": view["slot"],
            "endpoint": view["endpoint"],
            "allocated": view["allocated"],
            "allocated_by": view["allocated_by"],
            "tcp_port": b.tcp_port,
            "com_key": b.com_key,
            "health_state": b.health_state,
            "bound_device_id": b.bound_device_id if view["allocated"] else None,
        })
    return out


def register(session: Session, token: str, payload: dict) -> dict:
    """节点注册 + 端口上报（全量/增量 upsert）。响应带回 allocated。"""
    expire_allocations(session)
    node = _auth_and_resolve_node(session, token, payload.get("node_id"))
    now = utcnow()
    local_ip = payload.get("local_ip") or payload.get("ip")
    if local_ip:
        node.ip = str(local_ip).strip()
    if payload.get("sw_version"):
        node.sw_version = str(payload["sw_version"])
    if payload.get("caps") is not None:
        node.caps_json = payload["caps"]
    node.last_heartbeat_at = now
    node.status = "online"

    touched = []
    matches = []
    for port in payload.get("ports") or []:
        if port.get("slot") is not None and int(port["slot"]) <= 0:
            raise DomainError("COMRES_BAD_SLOT", "slot 必须为正整数")
        binding = _upsert_comres_binding(session, node, port, node.ip)
        m = _apply_auto_match(session, binding, port, node.ip)
        matches.append(m)
        touched.append(binding)

    _audit(session, node.node_id, "comres_register", actor=node.ip,
           detail={"ports": len(touched), "matches": matches})
    session.commit()
    return {
        "ok": True,
        "node_id": node.node_id,
        "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
        "ports": _ports_response(touched),
    }


def heartbeat(session: Session, token: str, payload: dict) -> dict:
    """周期心跳（可带端口增量）。已分配端口仍回 allocated=true 供节点停报。"""
    expire_allocations(session)
    node = _auth_and_resolve_node(session, token, payload.get("node_id"))
    now = utcnow()
    local_ip = payload.get("local_ip") or payload.get("ip")
    if local_ip:
        node.ip = str(local_ip).strip()
    node.last_heartbeat_at = now
    node.status = "online"

    touched = []
    for port in payload.get("ports") or []:
        binding = _upsert_comres_binding(session, node, port, node.ip)
        # 未占用才重新匹配；已占用保持
        if not _is_allocated(binding):
            _apply_auto_match(session, binding, port, node.ip)
        touched.append(binding)

    # 无 ports 增量时仍回该节点全部资源的 allocated，便于节点同步停心跳
    if not touched:
        touched = (session.query(WireLogPortBinding)
                   .filter_by(node_id=node.node_id).all())

    session.commit()
    return {
        "ok": True,
        "node_id": node.node_id,
        "ports": _ports_response(touched),
    }


def unregister(session: Session, token: str, payload: dict) -> dict:
    """按 node_id + ip + com + endpoint 精确撤销，不整节点清空。"""
    expire_allocations(session)
    node = _auth_and_resolve_node(session, token, payload.get("node_id"))
    local_ip = payload.get("local_ip") or payload.get("ip") or node.ip

    ports = payload.get("ports")
    if not ports:
        # 单端口扁平字段也接受
        if payload.get("com") or payload.get("endpoint") or payload.get("com_key"):
            ports = [payload]
        else:
            raise DomainError("COMRES_NO_PORTS", "unregister 需要 ports[] 或 com/endpoint")

    removed = []
    for port in ports:
        ep = port.get("endpoint")
        com = port.get("com") or port.get("com_name") or port.get("tty")
        ip = port.get("ip") or local_ip
        binding = _find_binding(
            session, node.node_id,
            com_key=port.get("com_key"),
            slot=port.get("slot"),
            com=com, endpoint=ep, ip=ip,
        )
        if binding is None:
            continue
        removed.append({
            "com": binding.com_name, "com_key": binding.com_key,
            "endpoint": binding.endpoint, "slot": binding.slot,
        })
        session.delete(binding)

    _audit(session, node.node_id, "comres_unregister", actor=node.ip,
           detail={"removed": removed, "requested": len(ports)})
    session.commit()
    return {"ok": True, "node_id": node.node_id, "removed": removed}


def allocated_heartbeat(session: Session, payload: dict,
                        actor: str | None = None) -> dict:
    """占用方续约。按 node_id + (com_key|endpoint|com) 定位资源，刷新 TTL。"""
    expire_allocations(session)
    node_id = payload.get("node_id")
    if not node_id:
        raise DomainError("COMRES_NO_NODE", "allocated-heartbeat 需要 node_id")
    node = session.get(WireLogNode, node_id)
    if node is None:
        raise DomainError("COMRES_NODE_NOT_FOUND", f"节点不存在: {node_id}")

    binding = _find_binding(
        session, node_id,
        com_key=payload.get("com_key"),
        slot=payload.get("slot"),
        com=payload.get("com") or payload.get("com_name"),
        endpoint=payload.get("endpoint"),
        ip=payload.get("ip") or payload.get("local_ip") or node.ip,
    )
    if binding is None:
        raise DomainError("COMRES_RESOURCE_NOT_FOUND", "资源不存在")

    if not binding.bound_device_id and not binding.allocated_by:
        raise DomainError("COMRES_NOT_ALLOCATED", "资源未分配，无法续约")

    by = payload.get("allocated_by") or actor or binding.allocated_by or "lease-holder"
    ttl = payload.get("ttl_s")
    _mark_allocated(binding, by=str(by), ttl_s=ttl)
    _audit(session, node_id, "comres_allocated_heartbeat",
           actor=str(by), detail={
               "com": binding.com_name, "com_key": binding.com_key,
               "allocated_expire": binding.allocated_expire.isoformat()
               if binding.allocated_expire else None,
               "ttl_s": int(ttl if ttl is not None else ALLOCATED_TTL_S),
           })
    session.commit()
    return {
        "ok": True,
        "node_id": node_id,
        "com": binding.com_name,
        "com_key": binding.com_key,
        "allocated": True,
        "allocated_by": binding.allocated_by,
        "allocated_expire": binding.allocated_expire.isoformat()
        if binding.allocated_expire else None,
        "bound_device_id": binding.bound_device_id,
    }


def _resource_item(b: WireLogPortBinding, node: WireLogNode | None, allocated: bool) -> dict:
    return {
        "node_id": b.node_id,
        "node_ip": node.ip if node else None,
        "com": b.com_name,
        "com_key": b.com_key,
        "tcp_port": b.tcp_port,
        "endpoint": b.endpoint,
        "slot": b.slot,
        "health_state": b.health_state,
        "baud": b.baud,
        "allocated": allocated,
        "allocated_by": b.allocated_by if allocated else None,
        "allocated_at": b.allocated_at.isoformat() if allocated and b.allocated_at else None,
        "allocated_expire": (
            b.allocated_expire.isoformat() if allocated and b.allocated_expire else None
        ),
        "device_id": b.bound_device_id if allocated else None,
        "bound_device_id": b.bound_device_id if allocated else None,
    }


def list_resources(session: Session) -> dict:
    """资源视图：已分配 / 资源池 两组。"""
    expire_allocations(session)
    nodes = {n.node_id: n for n in session.query(WireLogNode).all()}
    rows = (session.query(WireLogPortBinding)
            .order_by(WireLogPortBinding.node_id, WireLogPortBinding.slot)
            .all())
    now = utcnow()
    allocated, pool = [], []
    for b in rows:
        node = nodes.get(b.node_id)
        if _is_allocated(b, now):
            allocated.append(_resource_item(b, node, True))
        else:
            # 池内：无有效占用；若有过期残留字段，视图仍不带 device_id
            allocated_flag = False
            item = _resource_item(b, node, allocated_flag)
            # 未占用但可能仍挂着历史 bound（理论不应）；池强制不带 device
            item["device_id"] = None
            item["bound_device_id"] = None
            pool.append(item)
    return {"allocated": allocated, "pool": pool}
