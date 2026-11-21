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

from app.constants import DeviceStatus, HealthLevel, RegisterChannel
from app.domain.catalog import DomainError
from app.domain import wirelog
from app.models import Device, WireLogNode, WireLogPortBinding
from app.serial_tcp import SerialConfigError, parse_endpoint
from app.utils import new_device_id, utcnow

# 未分配资源：节点侧默认心跳间隔（契约 §2.7）
HEARTBEAT_INTERVAL_S = 3600
# 已分配资源：占用方续约缺省 TTL（契约 §2.7；Q4 待拍板，先按 15 分钟）
ALLOCATED_TTL_S = 15 * 60
# auto:<basis> 占用：≥ 2 倍心跳，覆盖一次漏报仍保持专属口
AUTO_ALLOCATED_TTL_S = 2 * HEARTBEAT_INTERVAL_S
# 节点超 2 倍心跳无上报 → stale，释放该节点全部 auto 占用
NODE_STALE_S = 2 * HEARTBEAT_INTERVAL_S

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
    """占用有效：allocated_by 存在且未过期（与 bound 解耦，支撑三态视图）。"""
    now = now or utcnow()
    if not binding.allocated_by:
        return False
    if binding.allocated_expire is not None and binding.allocated_expire <= now:
        return False
    return True


def _is_auto_allocation(binding: WireLogPortBinding) -> bool:
    return bool(binding.allocated_by) and str(binding.allocated_by).startswith("auto:")


def _is_online_binding(binding: WireLogPortBinding) -> bool:
    """在线视图口径：health_state=offline 的端口从 resources 三态中剔除。"""
    return (binding.health_state or "").lower() != "offline"


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
        "bound_device_id": binding.bound_device_id,
    }


def _clear_allocation(binding: WireLogPortBinding) -> dict:
    """解除占用，保留 bound_device_id；返回审计 detail 片段。"""
    detail = {
        "com": binding.com_name, "com_key": binding.com_key, "endpoint": binding.endpoint,
        "was_allocated_by": binding.allocated_by,
        "was_device_id": binding.bound_device_id,
        "allocated_expire": (
            binding.allocated_expire.isoformat() if binding.allocated_expire else None
        ),
        "kept_bound_device_id": binding.bound_device_id,
    }
    binding.allocated_by = None
    binding.allocated_at = None
    binding.allocated_expire = None
    return detail


def expire_allocations(session: Session) -> list[dict]:
    """超时未续约 → 解除占用；节点 stale → 释放该节点全部 auto 占用。

    保留 bound_device_id，落入「已绑定未分配」。人工/资源池占用仍仅按 allocated_expire。
    """
    now = utcnow()
    released = []
    rows = (session.query(WireLogPortBinding)
            .filter(WireLogPortBinding.allocated_expire.isnot(None))
            .filter(WireLogPortBinding.allocated_expire <= now)
            .all())
    for b in rows:
        detail = _clear_allocation(b)
        _audit(session, b.node_id, "comres_release_expired",
               result="ok", detail=detail)
        released.append({"node_id": b.node_id, "com_key": b.com_key, **detail})

    # 节点 stale：曾上报且超 2 倍心跳无再报 → 释放仍有效的 auto:* 占用
    stale_before = now - timedelta(seconds=NODE_STALE_S)
    stale_nodes = (
        session.query(WireLogNode)
        .filter(WireLogNode.last_heartbeat_at.isnot(None))
        .filter(WireLogNode.last_heartbeat_at <= stale_before)
        .all()
    )
    for node in stale_nodes:
        autos = (
            session.query(WireLogPortBinding)
            .filter_by(node_id=node.node_id)
            .filter(WireLogPortBinding.allocated_by.isnot(None))
            .all()
        )
        for b in autos:
            if not _is_auto_allocation(b):
                continue
            if not _is_allocated(b, now):
                continue
            detail = _clear_allocation(b)
            detail["reason"] = "node_stale"
            detail["last_heartbeat_at"] = (
                node.last_heartbeat_at.isoformat() if node.last_heartbeat_at else None
            )
            _audit(session, b.node_id, "comres_auto_release_stale",
                   result="ok", detail=detail)
            released.append({"node_id": b.node_id, "com_key": b.com_key, **detail})

    if released:
        session.commit()
    return released


def _mark_allocated(binding: WireLogPortBinding, by: str, ttl_s: int | None = None):
    now = utcnow()
    if ttl_s is not None:
        ttl = int(ttl_s)
    elif str(by).startswith("auto:"):
        ttl = AUTO_ALLOCATED_TTL_S
    else:
        ttl = ALLOCATED_TTL_S
    binding.allocated_by = by
    binding.allocated_at = now
    binding.allocated_expire = now + timedelta(seconds=ttl)


def _renew_auto_lease_if_needed(binding: WireLogPortBinding) -> None:
    """心跳/再上报时刷新 auto 占用，使其覆盖至下次 2 倍心跳窗口。"""
    if _is_allocated(binding) and _is_auto_allocation(binding):
        _mark_allocated(binding, by=binding.allocated_by)


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
    # 再次上报即恢复 online（含从 offline 恢复）；显式 health_state 优先
    if port.get("health_state"):
        binding.health_state = str(port["health_state"])
    else:
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


def _port_identity(port: dict) -> tuple[str | None, str | None, str | None]:
    """从 port 提取 (sn, mac, ip)；缺失为 None，不臆造。"""
    sn = (port.get("sn") or "").strip() or None
    mac = (port.get("mac") or port.get("shell_mac") or "").strip().lower() or None
    ip = (port.get("ip") or port.get("shell_ip") or "").strip() or None
    return sn, mac, ip


def _fill_device_missing(device: Device, port: dict) -> list[str]:
    """仅回填设备缺失字段；返回实际写入的字段名。"""
    sn, mac, ip = _port_identity(port)
    filled = []
    if sn and not device.sn:
        device.sn = sn
        filled.append("sn")
    if mac and not device.mac:
        device.mac = mac
        filled.append("mac")
    if ip and not device.ip:
        device.ip = ip
        filled.append("ip")
    return filled


def _create_device_from_port(session: Session, port: dict,
                             actor: str | None = None) -> Device:
    """port 带 sn 或 mac 且库中无匹配时建档；register_channel=comres_tcp。"""
    sn, mac, ip = _port_identity(port)
    if not sn and not mac:
        raise DomainError("COMRES_NO_IDENTITY", "自动建档需要 sn 或 mac")
    now = utcnow()
    device = Device(
        device_id=new_device_id(),
        display_name=f"comres-{sn or mac}",
        sn=sn,
        mac=mac,
        ip=ip,
        owner=actor or "system",
        status=DeviceStatus.PENDING,
        health_level=HealthLevel.L0,
        registered_by=actor,
        register_channel=RegisterChannel.COMRES_TCP,
        source="serial",
        last_seen_at=now,
        created_at=now,
    )
    session.add(device)
    session.flush()
    return device


def _other_binding_for_device(session: Session, device_id: str,
                              exclude_binding_id: int | None):
    """同一 device 是否已被其它端口绑定（冲突判定）。"""
    q = (session.query(WireLogPortBinding)
         .filter(WireLogPortBinding.bound_device_id == device_id))
    if exclude_binding_id is not None:
        q = q.filter(WireLogPortBinding.id != exclude_binding_id)
    return q.first()


def _apply_auto_match(session, binding: WireLogPortBinding, port: dict,
                      local_ip: str | None) -> dict:
    """匹配 / 自动建档 / 冲突进池；命中或建档则绑定并标记 allocated。"""
    # 已在有效占用中：不重复匹配抢占；auto 占用刷新保活窗口
    if _is_allocated(binding):
        _renew_auto_lease_if_needed(binding)
        return {
            "basis": "keep_allocated",
            "device_id": binding.bound_device_id,
            "allocated": True,
        }

    sn, mac, _ip = _port_identity(port)
    device, basis = _match_device(session, port, local_ip)

    # 未命中：有 sn/mac → 自动建档；否则进池
    if device is None:
        if not sn and not mac:
            _audit(session, binding.node_id, "comres_match",
                   result="miss", detail={
                       "com": binding.com_name, "com_key": binding.com_key,
                       "basis": "miss", "sn": sn, "mac": mac,
                       "ip": port.get("ip") or port.get("shell_ip") or local_ip,
                   })
            return {"basis": "miss", "device_id": None, "allocated": False}

        device = _create_device_from_port(session, port, actor=local_ip)
        basis = "created"
        binding.bound_device_id = device.device_id
        _mark_allocated(binding, by=f"auto:{basis}")
        _audit(session, binding.node_id, "comres_device_register",
               result="ok", detail={
                   "com": binding.com_name, "com_key": binding.com_key,
                   "basis": basis, "device_id": device.device_id,
                   "sn": device.sn, "mac": device.mac, "ip": device.ip,
                   "allocated_by": binding.allocated_by,
                   "allocated_expire": binding.allocated_expire.isoformat()
                   if binding.allocated_expire else None,
               })
        return {"basis": basis, "device_id": device.device_id, "allocated": True}

    # 已存在：回填缺失字段
    filled = _fill_device_missing(device, port)

    # 冲突：该 device 已被其它端口绑定 → 本端口进池，不覆盖
    other = _other_binding_for_device(session, device.device_id, binding.id)
    if other is not None:
        _audit(session, binding.node_id, "comres_device_register",
               result="conflict", detail={
                   "com": binding.com_name, "com_key": binding.com_key,
                   "basis": "conflict", "device_id": device.device_id,
                   "held_by_node": other.node_id, "held_by_com": other.com_name,
                   "held_by_com_key": other.com_key,
                   "sn": sn, "mac": mac, "filled": filled,
                   "warning": "device already bound elsewhere; left in pool",
               })
        return {
            "basis": "conflict",
            "device_id": None,
            "allocated": False,
            "conflict_device_id": device.device_id,
        }

    # 本端口已绑同一 device，或尚未绑定：幂等绑定 + 占用
    binding.bound_device_id = device.device_id
    _mark_allocated(binding, by=f"auto:{basis}")
    # sn/mac 路径记 comres_device_register；纯 IP 命中仍走 comres_match
    if basis in ("sn", "mac") or filled:
        _audit(session, binding.node_id, "comres_device_register",
               result="ok", detail={
                   "com": binding.com_name, "com_key": binding.com_key,
                   "basis": basis, "device_id": device.device_id,
                   "filled": filled,
                   "allocated_by": binding.allocated_by,
                   "allocated_expire": binding.allocated_expire.isoformat()
                   if binding.allocated_expire else None,
               })
    else:
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
            "bound_device_id": view["bound_device_id"],
        })
    return out


def _mark_absent_ports_offline(session: Session, node_id: str,
                               reported: list[WireLogPortBinding]) -> list[dict]:
    """本次 ports[] 未出现的既有绑定 → offline；保留绑定与设备关系。

    契约：register / 带 ports 的 heartbeat 为全量视图；空 ports 心跳不触发下线。
    有效占用（_is_allocated）的端口不置 offline——占用方续约期间宿主可停报存在性，
    物理在线仍归占用语义；未分配端口仍按「未上报即 offline」。
    """
    session.flush()
    reported_ids = {b.id for b in reported if b.id is not None}
    existing = (session.query(WireLogPortBinding)
                .filter_by(node_id=node_id).all())
    now = utcnow()
    offlined = []
    for b in existing:
        if b.id in reported_ids:
            continue
        was = b.health_state
        if (was or "").lower() == "offline":
            continue
        detail = {
            "com": b.com_name,
            "com_key": b.com_key,
            "endpoint": b.endpoint,
            "was_health_state": was,
        }
        # 已分配：保持原 health_state，不置 offline
        if _is_allocated(b, now):
            detail["allocated_by"] = b.allocated_by
            detail["allocated_expire"] = (
                b.allocated_expire.isoformat() if b.allocated_expire else None
            )
            _audit(session, node_id, "comres_port_skipped_allocated",
                   result="ok", detail=detail)
            continue
        b.health_state = "offline"
        _audit(session, node_id, "comres_port_offline", result="ok", detail=detail)
        offlined.append(detail)
    return offlined


def register(session: Session, token: str, payload: dict) -> dict:
    """节点注册 + 端口上报（全量 upsert）。响应带回 allocated。"""
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

    ports_in = payload.get("ports") or []
    touched = []
    matches = []
    for port in ports_in:
        if port.get("slot") is not None and int(port["slot"]) <= 0:
            raise DomainError("COMRES_BAD_SLOT", "slot 必须为正整数")
        binding = _upsert_comres_binding(session, node, port, node.ip)
        m = _apply_auto_match(session, binding, port, node.ip)
        matches.append(m)
        touched.append(binding)

    # 全量语义：未上报且未分配 → offline；已分配保持 health_state（不清绑定）
    if ports_in:
        _mark_absent_ports_offline(session, node.node_id, touched)

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
    """周期心跳。带 ports[] 时按全量视图：未出现且未分配 → offline。

    有效占用端口未上报时保持原 health_state（comres_port_skipped_allocated）。
    无 ports 时仅刷新节点心跳并回该节点全部资源 allocated（不停报）。
    """
    expire_allocations(session)
    node = _auth_and_resolve_node(session, token, payload.get("node_id"))
    now = utcnow()
    local_ip = payload.get("local_ip") or payload.get("ip")
    if local_ip:
        node.ip = str(local_ip).strip()
    node.last_heartbeat_at = now
    node.status = "online"

    ports_in = payload.get("ports") or []
    touched = []
    for port in ports_in:
        binding = _upsert_comres_binding(session, node, port, node.ip)
        # 未占用才重新匹配；已占用保持，auto 则续保活
        if _is_allocated(binding):
            _renew_auto_lease_if_needed(binding)
        else:
            _apply_auto_match(session, binding, port, node.ip)
        touched.append(binding)

    if ports_in:
        _mark_absent_ports_offline(session, node.node_id, touched)
    else:
        # 无 ports 增量时仍回该节点全部资源的 allocated，便于节点同步停心跳
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


def _resource_item(b: WireLogPortBinding, node: WireLogNode | None, *,
                   allocated: bool, include_device: bool) -> dict:
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
        "device_id": b.bound_device_id if include_device else None,
        "bound_device_id": b.bound_device_id if include_device else None,
        "state": (
            "allocated" if allocated
            else ("bound" if include_device and b.bound_device_id else "pool")
        ),
    }


def list_resources(session: Session) -> dict:
    """资源视图三态：已分配 / 已绑定未分配 / 纯池。

    在线视图契约：health_state=offline 的端口从 allocated/bound/pool 剔除
    （绑定与设备关系仍保留在库内；再次上报恢复 online 后重新出现）。
    """
    expire_allocations(session)
    nodes = {n.node_id: n for n in session.query(WireLogNode).all()}
    rows = (session.query(WireLogPortBinding)
            .order_by(WireLogPortBinding.node_id, WireLogPortBinding.slot)
            .all())
    now = utcnow()
    allocated, bound, pool = [], [], []
    for b in rows:
        if not _is_online_binding(b):
            continue
        node = nodes.get(b.node_id)
        if _is_allocated(b, now):
            allocated.append(_resource_item(b, node, allocated=True, include_device=True))
        elif b.bound_device_id:
            bound.append(_resource_item(b, node, allocated=False, include_device=True))
        else:
            pool.append(_resource_item(b, node, allocated=False, include_device=False))
    return {"allocated": allocated, "bound": bound, "pool": pool}
