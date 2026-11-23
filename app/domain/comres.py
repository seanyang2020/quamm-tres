"""COMRes 域：TCP COM 资源注册 / 解注册 / 心跳 / 自动匹配 / 占用续约。

契约 §2.7。复用 wl_node / wl_port_binding / wl_audit；与旧 wirelog 北向并存。
唯一键（t942）：node_id + com（com_name）；端点独占：ip 非空时 (ip, tcp_port) 全局互斥。
com_key = "{ip}:{tcp_port}" 仍作端点索引。

健康口径三层（勿合并）：
- bind_health：节点自报 health_state / shell_reachable（板侧本地串口自检）
- reach：TRES→endpoint TCP 连通性（probe_reach 写 reach_*）
- session：当前是否占用/打开（allocated / shell 会话）
"""
from __future__ import annotations

import errno
import json
import re
import socket
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
# 节点超 2 倍心跳无上报 → 释放该节点全部 auto 占用（与 prune 的 offline/stale 并存）
NODE_STALE_S = 2 * HEARTBEAT_INTERVAL_S

# 不进可分配池、资源池视图灰显
_INACTIVE_HEALTH = frozenset({"offline", "stale", "superseded"})

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


def _allocation_marker_active(binding: WireLogPortBinding, now=None) -> bool:
    """allocated_by 存在且未过期（含 auto:*；TTL 回收 / 续约用）。"""
    now = now or utcnow()
    if not binding.allocated_by:
        return False
    if binding.allocated_expire is not None and binding.allocated_expire <= now:
        return False
    return True


def _is_auto_allocation(binding: WireLogPortBinding) -> bool:
    return bool(binding.allocated_by) and str(binding.allocated_by).startswith("auto:")


def _is_allocated(binding: WireLogPortBinding, now=None) -> bool:
    """真实租用占用：非 auto:* 且 marker 未过期。

    F6：``auto:*`` 是自动绑定占位，展示/registrar 口径算 bound，不算已分配。
    """
    if not _allocation_marker_active(binding, now):
        return False
    return not _is_auto_allocation(binding)


def _is_bound(binding: WireLogPortBinding, now=None) -> bool:
    """已绑定：有 bound_device_id，或有效的 auto:* 占位。"""
    if binding.bound_device_id:
        return True
    return _is_auto_allocation(binding) and _allocation_marker_active(binding, now)


def _is_online_binding(binding: WireLogPortBinding) -> bool:
    """可分配/三态视图口径：offline/stale/superseded 不进 allocated/bound/pool。"""
    return (binding.health_state or "").lower() not in _INACTIVE_HEALTH


def _port_allocated_view(binding: WireLogPortBinding, now=None) -> dict:
    now = now or utcnow()
    allocated = _is_allocated(binding, now)
    bound = _is_bound(binding, now)
    marker = _allocation_marker_active(binding, now)
    return {
        "com": binding.com_name,
        "slot": binding.slot,
        "endpoint": binding.endpoint,
        "allocated": allocated,
        "bound": bound,
        # 保留 allocated_by 便于审计（含 auto:*），即使 allocated=false
        "allocated_by": binding.allocated_by if marker else None,
        "allocated_expire": (
            binding.allocated_expire.isoformat()
            if marker and binding.allocated_expire else None
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
    """超时未续约 → 解除占用（仅节点非 online）；节点失联 → 释放 auto 占用。

    t942：已分配端口只在「节点非 online 且 allocated TTL 到期」时回收；
    正常 online 节点上的续约窗口由占用方 allocated-heartbeat 维持，不因 TTL
    单独清掉（避免幽灵占用与误伤并存时的口径漂移）。
    保留 bound_device_id，落入「已绑定未分配」。
    """
    now = utcnow()
    released = []
    nodes = {n.node_id: n for n in session.query(WireLogNode).all()}
    rows = (session.query(WireLogPortBinding)
            .filter(WireLogPortBinding.allocated_expire.isnot(None))
            .filter(WireLogPortBinding.allocated_expire <= now)
            .all())
    for b in rows:
        node = nodes.get(b.node_id)
        if node is not None and (node.status or "") == "online":
            continue
        detail = _clear_allocation(b)
        detail["reason"] = "ttl_expired_node_not_online"
        _audit(session, b.node_id, "comres_release_expired",
               result="ok", detail=detail)
        released.append({"node_id": b.node_id, "com_key": b.com_key, **detail})

    # 节点失联：曾上报且超 2 倍心跳无再报 → 释放仍有效的 auto:* 占用
    stale_before = now - timedelta(seconds=NODE_STALE_S)
    stale_nodes = (
        session.query(WireLogNode)
        .filter(WireLogNode.last_heartbeat_at.isnot(None))
        .filter(WireLogNode.last_heartbeat_at <= stale_before)
        .all()
    )
    for node in stale_nodes:
        if (node.status or "") == "online":
            # online 但心跳已超时：仍可清 auto（与 prune 转 offline 互补）
            pass
        autos = (
            session.query(WireLogPortBinding)
            .filter_by(node_id=node.node_id)
            .filter(WireLogPortBinding.allocated_by.isnot(None))
            .all()
        )
        for b in autos:
            if not _is_auto_allocation(b):
                continue
            if not _allocation_marker_active(b, now):
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
    if _is_auto_allocation(binding) and _allocation_marker_active(binding):
        _mark_allocated(binding, by=binding.allocated_by)


def _fp_identity(port: dict) -> dict:
    """从 port.fingerprint.identity 取字典；缺失返回空 dict。"""
    fp = port.get("fingerprint")
    if not isinstance(fp, dict):
        return {}
    ident = fp.get("identity")
    return ident if isinstance(ident, dict) else {}


def _sn_variants(sn: str) -> list[str]:
    """SN 候选：原值 + 去掉 ``vid:pid:`` 前缀后的裸芯片序列号。

    standalone 的端口唯一键是 ``vid:pid:serial``（stable_key），而 device.sn 传统上存裸
    序列号；两边都试，避免同一个物理设备被建成两条 device 记录。
    """
    raw = (sn or "").strip()
    if not raw:
        return []
    out = [raw]
    if ":" in raw:
        tail = raw.rsplit(":", 1)[-1].strip()
        if tail and tail not in out:
            out.append(tail)
    return out


def _device_by_sn(session: Session, sn: str) -> "Device | None":
    for cand in _sn_variants(sn):
        device = session.query(Device).filter(Device.sn == cand).one_or_none()
        if device is not None:
            return device
    return None


def _sn_taken_by_other(session: Session, sn: str, device: "Device") -> bool:
    for cand in _sn_variants(sn):
        other = (session.query(Device)
                 .filter(Device.sn == cand, Device.device_id != device.device_id)
                 .first())
        if other is not None:
            return True
    return False


def _match_device(session: Session, port: dict, local_ip: str | None):
    """按 MAC → SN → IP 优先级匹配 device。返回 (device|None, basis)。

    候选顺序：fingerprint.identity.mac → port.mac/shell_mac → identity.sn →
    port.sn → identity.ip / port.ip / shell_ip / local_ip / endpoint host。
    来自 fingerprint.identity 的命中 basis 为 fp_mac / fp_sn / fp_ip。
    """
    ident = _fp_identity(port)

    fp_mac = (ident.get("mac") or "").strip().lower()
    if fp_mac:
        device = session.query(Device).filter(Device.mac.ilike(fp_mac)).one_or_none()
        if device is not None:
            return device, "fp_mac"

    mac = (port.get("mac") or port.get("shell_mac") or "").strip().lower()
    if mac and mac != fp_mac:
        device = session.query(Device).filter(Device.mac.ilike(mac)).one_or_none()
        if device is not None:
            return device, "mac"

    fp_sn = (ident.get("sn") or "").strip()
    if fp_sn:
        device = _device_by_sn(session, fp_sn)
        if device is not None:
            return device, "fp_sn"

    port_sn = (port.get("sn") or "").strip()
    # 上报方带了 usb_serial → port.sn 是适配器序列号，不参与设备匹配
    if port_sn and not port.get("usb_serial"):
        device = _device_by_sn(session, port_sn)
        if device is not None:
            return device, "sn"

    fp_ip = (ident.get("ip") or "").strip()
    port_ip = (port.get("ip") or port.get("shell_ip") or "").strip()
    fallback_ip = (local_ip or "").strip()
    endpoint_ip = ""
    if port.get("endpoint"):
        try:
            endpoint_ip, _ = parse_endpoint(str(port["endpoint"]))
            endpoint_ip = (endpoint_ip or "").strip()
        except SerialConfigError:
            endpoint_ip = ""

    # IP 候选：identity.ip 优先（fp_ip），否则 port/local/endpoint（ip）
    if fp_ip:
        device = session.query(Device).filter(Device.ip == fp_ip).one_or_none()
        if device is not None:
            return device, "fp_ip"

    for cand in (port_ip, fallback_ip, endpoint_ip):
        if cand and cand != fp_ip:
            device = session.query(Device).filter(Device.ip == cand).one_or_none()
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


def _endpoint_online_window_s() -> int:
    """对方 last_seen「仍算更新」的窗口：与 prune stale 口径一致。"""
    from app.domain.comres_prune import stale_timeout_s
    return stale_timeout_s()


def _holder_endpoint_active(other: WireLogPortBinding,
                            other_node: WireLogNode | None,
                            *, now=None, window_s: int | None = None) -> bool:
    """占位方是否仍构成真冲突（F5）。

    仅当：节点 online，且 binding 非 inactive，且 last_checked_at 在活跃窗口内。
    """
    now = now or utcnow()
    window_s = window_s if window_s is not None else _endpoint_online_window_s()
    if other_node is None or (other_node.status or "") != "online":
        return False
    hs = (other.health_state or "").lower()
    if hs in _INACTIVE_HEALTH:
        return False
    checked = other.last_checked_at
    if checked is None:
        return False
    return (now - checked).total_seconds() <= window_s


def _resolve_endpoint_exclusivity(session: Session, node: WireLogNode,
                                  host: str | None, tcp_port: int | None,
                                  com_key: str | None) -> str | None:
    """端点独占：ip 非空时 (ip, tcp_port) 全局唯一。

    返回 None 表示可进池；返回 \"endpoint_conflict\" 表示对方仍活跃占位。
    F5：对方非 online / health 非活跃 / last_checked 过期 → 接管（superseded +
    ``comres_endpoint_takeover``）；已 superseded 的不重复写 takeover（幂等）。
    """
    if not host or tcp_port is None or not com_key:
        return None
    others = (
        session.query(WireLogPortBinding)
        .filter(WireLogPortBinding.com_key == com_key)
        .filter(WireLogPortBinding.node_id != node.node_id)
        .all()
    )
    if not others:
        return None

    now = utcnow()
    window = _endpoint_online_window_s()
    conflict = False
    for other in others:
        hs = (other.health_state or "").lower()
        if hs == "superseded":
            continue
        other_node = session.get(WireLogNode, other.node_id)
        if _holder_endpoint_active(other, other_node, now=now, window_s=window):
            conflict = True
            continue
        # 失效占位 → 接管
        was = other.health_state
        node_status = other_node.status if other_node else None
        reason = "holder_not_online"
        if other_node is not None and (other_node.status or "") == "online":
            if hs in _INACTIVE_HEALTH:
                reason = "holder_inactive_health"
            else:
                reason = "holder_last_checked_stale"
        other.health_state = "superseded"
        _audit(session, other.node_id, "comres_endpoint_takeover",
               actor=node.node_id, result="ok", detail={
                   "com_key": com_key,
                   "com": other.com_name,
                   "ip": host,
                   "tcp_port": int(tcp_port),
                   "by_node": node.node_id,
                   "was_health_state": was,
                   "holder_node_status": node_status,
                   "holder_last_checked_at": (
                       other.last_checked_at.isoformat()
                       if other.last_checked_at else None
                   ),
                   "reason": reason,
               })
    return "endpoint_conflict" if conflict else None


def _upsert_comres_binding(session, node: WireLogNode, port: dict,
                           local_ip: str | None) -> WireLogPortBinding | dict:
    """按 (node_id, com) upsert；端点冲突时返回 dict(reason=endpoint_conflict)。

    保留 slot / com_key 兼容旧唯一约束。
    """
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

    com = port.get("com") or port.get("port_name") or port.get("com_name") or port.get("tty")
    com_name = str(com).strip() if com else None

    # 端点独占：冲突则本端口不进池
    conflict = _resolve_endpoint_exclusivity(session, node, host, tcp_port, com_key)
    if conflict:
        _audit(session, node.node_id, "comres_endpoint_conflict",
               result="conflict", detail={
                   "com": com_name, "com_key": com_key, "endpoint": ep_store,
                   "reason": conflict,
               })
        return {
            "com": com_name,
            "slot": port.get("slot"),
            "endpoint": ep_store,
            "allocated": False,
            "allocated_by": None,
            "tcp_port": int(tcp_port) if tcp_port is not None else None,
            "com_key": com_key,
            "health_state": None,
            "bound_device_id": None,
            "reason": conflict,
        }

    slot_raw = port.get("slot")
    binding = None
    # t942：优先按 (node_id, com) 定位，同 com 覆盖
    if com_name:
        binding = (
            session.query(WireLogPortBinding)
            .filter_by(node_id=node.node_id)
            .filter(WireLogPortBinding.com_name == com_name)
            .one_or_none()
        )
    if binding is None and com_key:
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

    if com_name:
        binding.com_name = com_name
    if port.get("tty"):
        binding.tty_name = str(port["tty"]).strip()
    elif com_name and not binding.tty_name:
        binding.tty_name = com_name
    if port.get("baud") is not None:
        baud = int(port["baud"])
        if baud <= 0:
            raise DomainError("COMRES_BAD_BAUD", f"baud 必须 > 0: {baud}")
        binding.baud = baud
    # 再次上报即恢复 online（含从 offline/stale/superseded 恢复）；显式 health_state 优先
    if port.get("health_state"):
        binding.health_state = str(port["health_state"])
    else:
        binding.health_state = "online"
    if port.get("probe") is not None:
        binding.probe_json = json.dumps(port["probe"], ensure_ascii=False)[:8000]
    # 顶层 mac/ip 仍按既有口径写入；fingerprint.identity 仅在 shell_* 为空时回填
    if port.get("shell_mac") or port.get("mac"):
        binding.shell_mac = str(port.get("shell_mac") or port.get("mac")).strip().lower()
    if port.get("shell_ip") or port.get("ip"):
        binding.shell_ip = str(port.get("shell_ip") or port.get("ip")).strip()
    if "shell_reachable" in port:
        binding.shell_reachable = bool(port["shell_reachable"])
    _persist_fingerprint(session, binding, port)
    _enrich_port_from_fingerprint(port)
    binding.last_checked_at = utcnow()
    return binding


def _port_identity(port: dict) -> tuple[str | None, str | None, str | None]:
    """从 port 提取 (sn, mac, ip)；优先 fingerprint.identity，缺失为 None。

    身份优先级：fingerprint.identity.sn（板子 shell 读到的设备 SN）> 显式
    ``device_sn`` > ``port.sn``。但上报方若带了 ``usb_serial``，说明它已明确区分
    「适配器序列号 vs 设备序列号」，此时 ``port.sn`` 是适配器序列号，不算设备身份。
    """
    ident = _fp_identity(port)
    port_sn = (port.get("sn") or "").strip()
    if port_sn and port.get("usb_serial"):
        port_sn = ""
    sn = (ident.get("sn") or port.get("device_sn") or port_sn).strip() or None
    mac = (ident.get("mac") or port.get("mac") or port.get("shell_mac") or "").strip().lower() or None
    ip = (ident.get("ip") or port.get("ip") or port.get("shell_ip") or "").strip() or None
    return sn, mac, ip


def _enrich_port_from_fingerprint(port: dict) -> None:
    """兼容旧字段：identity 有值时回填 port.sn/mac/ip（仅原值为空时）。"""
    ident = _fp_identity(port)
    if not ident:
        return
    for key in ("sn", "mac", "ip"):
        cur = (port.get(key) or "").strip()
        if cur:
            continue
        val = (ident.get(key) or "").strip()
        if not val:
            continue
        port[key] = val.lower() if key == "mac" else val


def _fp_leaf_values(obj, prefix: str = "") -> dict[str, str]:
    """扁平化 fingerprint 叶子字符串；用于审计 cleared_fields。"""
    out: dict[str, str] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                out.update(_fp_leaf_values(v, path))
            elif v is None:
                continue
            else:
                s = str(v).strip()
                if s:
                    out[path] = s
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            path = f"{prefix}[{i}]"
            if isinstance(v, (dict, list)):
                out.update(_fp_leaf_values(v, path))
            elif v is None:
                continue
            else:
                s = str(v).strip()
                if s:
                    out[path] = s
    return out


def _fp_cleared_fields(old_fp: dict | None, new_fp: dict | None) -> list[str]:
    """返回从有值变为空的字段路径（避免审计写全文）。"""
    old_leaves = _fp_leaf_values(old_fp or {})
    new_leaves = _fp_leaf_values(new_fp or {})
    return sorted(k for k in old_leaves if k not in new_leaves)


def _parse_fingerprint_json(raw: str | None):
    if not raw:
        return None
    try:
        val = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _fingerprint_has_content(fp: dict) -> bool:
    """指纹必须有身份或系统字段才算有效；空壳（只有 schema/verdict）一律不落库。"""
    if not isinstance(fp, dict):
        return False
    ident = fp.get("identity") if isinstance(fp.get("identity"), dict) else {}
    sysd = fp.get("system") if isinstance(fp.get("system"), dict) else {}
    for key in ("mac", "sn", "ip"):
        if str(ident.get(key) or "").strip():
            return True
    for key in ("os", "kernel", "hostname", "project", "model", "build_info",
                "uname", "proc_version"):
        if str(sysd.get(key) or "").strip():
            return True
    return bool(fp.get("net"))


def _persist_fingerprint(session: Session, binding: WireLogPortBinding,
                         port: dict) -> None:
    """带有效 fingerprint 则落库；空壳/缺失保留旧值。identity 回填 shell_*（仅空时）。"""
    fp = port.get("fingerprint")
    if fp is None:
        return
    if not isinstance(fp, dict):
        return
    if not _fingerprint_has_content(fp):
        return

    new_json = json.dumps(fp, ensure_ascii=False)
    if len(new_json) > 64000:
        new_json = new_json[:64000]
    old_raw = binding.fingerprint_json
    if old_raw == new_json:
        # 仍尝试回填 shell_*（可能此前未落）
        _backfill_shell_from_identity(binding, fp)
        return

    old_fp = _parse_fingerprint_json(old_raw)
    cleared = _fp_cleared_fields(old_fp, fp)
    binding.fingerprint_json = new_json
    _backfill_shell_from_identity(binding, fp)
    _audit(session, binding.node_id, "comres_fingerprint_update",
           result="ok", detail={
               "com": binding.com_name,
               "com_key": binding.com_key,
               "slot": binding.slot,
               "first": old_raw is None,
               "cleared_fields": cleared,
               "verdict": fp.get("verdict"),
               "schema": fp.get("schema"),
           })


def _backfill_shell_from_identity(binding: WireLogPortBinding, fp: dict) -> None:
    ident = fp.get("identity") if isinstance(fp.get("identity"), dict) else {}
    mac = (ident.get("mac") or "").strip().lower()
    ip = (ident.get("ip") or "").strip()
    if mac and not binding.shell_mac:
        binding.shell_mac = mac
    if ip and not binding.shell_ip:
        binding.shell_ip = ip


def _fill_device_missing(session: Session, device: Device, port: dict) -> list[str]:
    """仅回填设备缺失字段；返回实际写入的字段名。"""
    sn, mac, ip = _port_identity(port)
    filled = []
    if sn and not device.sn and not _sn_taken_by_other(session, sn, device):
        device.sn = _sn_variants(sn)[-1]
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
    """port 带设备身份（mac，或 fingerprint/device_sn 的 SN）时建档。

    ``port.sn`` 是 USB 适配器序列号，不算身份，避免造出 comres-<适配器SN> 这类假设备。
    """
    sn, mac, ip = _port_identity(port)
    if not sn and not mac:
        raise DomainError("COMRES_NO_IDENTITY", "自动建档需要 sn 或 mac")
    if sn:
        existed = _device_by_sn(session, sn)
        if existed is not None:
            return existed
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


def _binding_identity_drifted(session, binding: WireLogPortBinding,
                              port: dict) -> bool:
    """端口上报的设备身份，和当前绑定设备是否已经对不上。

    - 有 MAC：MAC 不同即漂移（绑定设备 MAC 为空也算，交给重新匹配决定）；
    - 无 MAC 有 IP：IP 不同即漂移；
    - 都没有：不算漂移（沿用旧绑定）。
    """
    if not binding.bound_device_id:
        return False
    dev = session.get(Device, binding.bound_device_id)
    if dev is None:
        return False
    mac = (port.get("mac") or port.get("shell_mac") or "").strip().lower()
    if not mac:
        ident = _fp_identity(port)
        mac = (ident.get("mac") or "").strip().lower()
    if mac:
        return (dev.mac or "").strip().lower() != mac
    ip = (port.get("ip") or port.get("shell_ip") or "").strip()
    if ip:
        return (dev.ip or "").strip() != ip
    return False


def _retire_superseded_placeholder(session, device_id: str) -> None:
    """被更强身份顶掉的 comres 自动占位设备（comres-<适配器SN>）标记 retired。

    只处理 comres_tcp 通道、名字是 comres- 前缀、且当前没有任何端口绑定的占位，
    真设备（有 MAC / 有实际业务字段）不动。
    """
    dev = session.get(Device, device_id)
    if dev is None or dev.register_channel != RegisterChannel.COMRES_TCP:
        return
    if not str(dev.display_name or "").startswith("comres-"):
        return
    others = (session.query(WireLogPortBinding)
              .filter(WireLogPortBinding.bound_device_id == device_id).count())
    if others:
        return
    dev.status = DeviceStatus.RETIRED
    dev.retired_at = utcnow()
    _audit(session, "", "comres_placeholder_retired", result="ok",
           detail={"device_id": device_id, "display_name": dev.display_name})


def _is_comres_placeholder(session, device_id: str | None) -> bool:
    """该绑定是否指向 auto 建出来的 comres-<适配器SN> 占位设备。"""
    if not device_id:
        return False
    dev = session.query(Device).filter(Device.device_id == device_id).first()
    if dev is None:
        return False
    channel = str(getattr(dev, "register_channel", "") or "")
    name = str(getattr(dev, "display_name", "") or "")
    return channel.startswith("comres") or name.startswith("comres-")


def _retire_orphan_placeholders(session, usb_serial: str | None) -> None:
    """清掉历史上以适配器 SN 造出来的孤儿占位（无任何端口绑定）。"""
    serial = str(usb_serial or "").rsplit(":", 1)[-1].strip()
    if not serial:
        return
    rows = (session.query(Device)
            .filter(Device.register_channel == RegisterChannel.COMRES_TCP)
            .filter(Device.sn == serial).all())
    for dev in rows:
        if str(dev.status) == str(DeviceStatus.RETIRED):
            continue
        bound = (session.query(WireLogPortBinding)
                 .filter(WireLogPortBinding.bound_device_id == dev.device_id).count())
        if bound:
            continue
        dev.status = DeviceStatus.RETIRED
        dev.retired_at = utcnow()
        _audit(session, "", "comres_placeholder_retired", result="ok",
               detail={"device_id": dev.device_id, "display_name": dev.display_name,
                       "reason": "orphan_serial", "serial": serial})


def _apply_auto_match(session, binding: WireLogPortBinding, port: dict,
                      local_ip: str | None) -> dict:
    """匹配 / 自动建档 / 冲突进池；命中或建档则绑定并标记 auto:* 占位。

    F6：auto 绑定响应 ``allocated=false, bound=true``，避免 registrar 误跳探活。
    """
    # 真实租用：不抢占
    if _is_allocated(binding):
        return {
            "basis": "keep_allocated",
            "device_id": binding.bound_device_id,
            "allocated": True,
            "bound": True,
        }
    _enrich_port_from_fingerprint(port)

    # 已有 auto 占位：身份没变才 keep_bound；探到更准的身份（MAC/IP）或换了设备，
    # 必须重新匹配，否则一次弱身份（只剩适配器 SN）会把错误绑定锁死到 TTL 结束。
    prev_device_id = binding.bound_device_id
    drifted = _binding_identity_drifted(session, binding, port)
    sn, mac, _ip = _port_identity(port)
    # 上报方已把「适配器SN」与设备身份分开，但这次没有设备身份：
    # 原绑定若只是 comres-<适配器SN> 占位，说明它是拿桥的SN冒充设备，必须清掉。
    stale_placeholder = bool(
        port.get("usb_serial") and not sn and not mac and prev_device_id
        and _is_comres_placeholder(session, prev_device_id)
    )
    if (
        _is_auto_allocation(binding)
        and _allocation_marker_active(binding)
        and not drifted
        and not stale_placeholder
    ):
        _renew_auto_lease_if_needed(binding)
        _retire_orphan_placeholders(session, port.get("usb_serial"))
        return {
            "basis": "keep_bound",
            "device_id": binding.bound_device_id,
            "allocated": False,
            "bound": True,
        }

    device, basis = _match_device(session, port, local_ip)

    def _keep_prev(why: str) -> dict:
        """重新匹配没结果/冲突时，保住原绑定，别把在用端口踢成未绑定。"""
        _renew_auto_lease_if_needed(binding)
        _audit(session, binding.node_id, "comres_match", result="keep_prev",
               detail={"com": binding.com_name, "com_key": binding.com_key,
                       "basis": why, "device_id": prev_device_id,
                       "sn": sn, "mac": mac})
        return {"basis": "keep_prev", "device_id": prev_device_id,
                "allocated": False, "bound": True}

    # 未命中：漂移场景保旧绑定；有设备身份 → 自动建档；否则进池
    if device is None:
        if drifted and prev_device_id:
            return _keep_prev("identity_drift_no_match")
        if port.get("usb_serial") and not sn and not mac:
            # 上报方明确区分了「适配器SN」，但这次没拿到设备身份：不造设备、不保留旧占位
            if prev_device_id:
                # 先解绑再 retire：占位设备只有在没有任何端口引用时才可 retired
                binding.bound_device_id = None
                binding.allocated_by = None
                binding.allocated_expire = None
                session.flush()
                _retire_superseded_placeholder(session, prev_device_id)
            _audit(session, binding.node_id, "comres_match", result="unknown_device",
                   detail={"com": binding.com_name, "com_key": binding.com_key,
                           "usb_serial": port.get("usb_serial")})
            return {"basis": "unknown_device", "device_id": None,
                    "allocated": False, "bound": False}
        if not sn and not mac:
            _audit(session, binding.node_id, "comres_match",
                   result="miss", detail={
                       "com": binding.com_name, "com_key": binding.com_key,
                       "basis": "miss", "sn": sn, "mac": mac,
                       "ip": port.get("ip") or port.get("shell_ip") or local_ip,
                   })
            return {"basis": "miss", "device_id": None,
                    "allocated": False, "bound": False}

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
        return {"basis": basis, "device_id": device.device_id,
                "allocated": False, "bound": True}

    # 已存在：回填缺失字段
    filled = _fill_device_missing(session, device, port)

    # 冲突：该 device 已被其它端口绑定 → 本端口进池，不覆盖
    other = _other_binding_for_device(session, device.device_id, binding.id)
    if other is not None and drifted and prev_device_id:
        return _keep_prev(f"identity_drift_conflict:{other.node_id}/{other.com_name}")
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
            "bound": False,
            "conflict_device_id": device.device_id,
        }

    # 本端口已绑同一 device，或尚未绑定：幂等绑定 + auto 占位
    binding.bound_device_id = device.device_id
    if prev_device_id and prev_device_id != device.device_id:
        # 先落一次 flush 再回收占位，否则旧占位还被本端口引用着（count=1）
        session.flush()
        _retire_superseded_placeholder(session, prev_device_id)
    _mark_allocated(binding, by=f"auto:{basis}")
    _retire_orphan_placeholders(session, port.get("usb_serial"))
    # sn/mac 路径记 comres_device_register；纯 IP 命中仍走 comres_match
    if basis in ("sn", "mac", "fp_sn", "fp_mac") or filled:
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
    return {"basis": basis, "device_id": device.device_id,
            "allocated": False, "bound": True}


def _auth_and_resolve_node(session, token: str, payload_node_id: str | None):
    node = wirelog._auth_node(session, token)
    if payload_node_id and payload_node_id != node.node_id:
        raise DomainError(
            "COMRES_NODE_MISMATCH",
            f"payload.node_id={payload_node_id} 与 token 节点 {node.node_id} 不一致",
        )
    return node


def _ports_response(items: list) -> list[dict]:
    """bindings 或冲突 dict 混排 → 响应 ports[]。"""
    now = utcnow()
    out = []
    for item in items:
        if isinstance(item, dict) and item.get("reason"):
            item.setdefault("allocated", False)
            item.setdefault("bound", False)
            out.append(item)
            continue
        b = item
        view = _port_allocated_view(b, now)
        out.append({
            "com": view["com"],
            "slot": view["slot"],
            "endpoint": view["endpoint"],
            "allocated": view["allocated"],
            "bound": view["bound"],
            "allocated_by": view["allocated_by"],
            "allocated_expire": view["allocated_expire"],
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
    """节点注册 + 端口上报（全量 upsert）。响应带回 allocated。

    端点冲突的端口以 reason=endpoint_conflict 出现在 ports[]，不进池。
    """
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
        if isinstance(binding, dict):
            matches.append({"basis": "endpoint_conflict", "reason": binding.get("reason")})
            touched.append(binding)
            continue
        m = _apply_auto_match(session, binding, port, node.ip)
        matches.append(m)
        touched.append(binding)

    # 全量语义：未上报且未分配 → offline；已分配保持 health_state（不清绑定）
    real_bindings = [b for b in touched if not isinstance(b, dict)]
    if ports_in:
        _mark_absent_ports_offline(session, node.node_id, real_bindings)

    _audit(session, node.node_id, "comres_register", actor=node.ip,
           detail={"ports": len(real_bindings), "matches": matches})
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
        if isinstance(binding, dict):
            touched.append(binding)
            continue
        # 真实租用保持；auto 占位续 TTL；未占位则匹配
        _apply_auto_match(session, binding, port, node.ip)
        touched.append(binding)

    real_bindings = [b for b in touched if not isinstance(b, dict)]
    if ports_in:
        _mark_absent_ports_offline(session, node.node_id, real_bindings)
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


def _inactive_reason(b: WireLogPortBinding, node: WireLogNode | None) -> str:
    hs = (b.health_state or "").lower()
    if hs == "superseded":
        return "endpoint_superseded"
    if hs == "stale":
        return "heartbeat_stale"
    if hs == "offline":
        return "port_offline"
    if node and (node.status or "") == "dormant":
        return "node_dormant"
    return hs or "inactive"


def _resource_item(b: WireLogPortBinding, node: WireLogNode | None, *,
                   allocated: bool, bound: bool, include_device: bool,
                   inactive: bool = False, now=None) -> dict:
    now = now or utcnow()
    hs = (b.health_state or "").lower()
    if inactive:
        state = hs if hs in _INACTIVE_HEALTH else "inactive"
    elif allocated:
        state = "allocated"
    elif bound:
        state = "bound"
    else:
        state = "pool"
    marker = _allocation_marker_active(b, now)
    item = {
        "node_id": b.node_id,
        "node_ip": node.ip if node else None,
        "node_status": node.status if node else None,
        "com": b.com_name,
        "com_key": b.com_key,
        "tcp_port": b.tcp_port,
        "endpoint": b.endpoint,
        "slot": b.slot,
        "health_state": b.health_state,
        "baud": b.baud,
        "allocated": allocated,
        "bound": bound,
        # 审计保留 allocated_by（含 auto:*），即使 allocated=false
        "allocated_by": b.allocated_by if marker else None,
        "allocated_at": (
            b.allocated_at.isoformat() if marker and b.allocated_at else None
        ),
        "allocated_expire": (
            b.allocated_expire.isoformat()
            if marker and b.allocated_expire else None
        ),
        "device_id": b.bound_device_id if include_device else None,
        "bound_device_id": b.bound_device_id if include_device else None,
        "state": state,
        "inactive": inactive,
        # reach_*：None/空 = 未测（勿当成失败）
        "reach_ok": b.reach_ok,
        "reach_error": b.reach_error,
        "reach_checked_at": (
            b.reach_checked_at.isoformat() if b.reach_checked_at else None
        ),
        "fingerprint": _parse_fingerprint_json(b.fingerprint_json),
        "fp_verdict": None,
    }
    fp = item["fingerprint"]
    if isinstance(fp, dict):
        item["fp_verdict"] = fp.get("verdict")
        # 便于资源池 sn/mac 列：优先 identity，其次 shell_*
        ident = fp.get("identity") if isinstance(fp.get("identity"), dict) else {}
        if not item.get("sn"):
            item["sn"] = (ident.get("sn") or None)
        if not item.get("mac"):
            item["mac"] = (ident.get("mac") or b.shell_mac or None)
        if not item.get("ip"):
            item["ip"] = (ident.get("ip") or b.shell_ip or None)
    else:
        if b.shell_mac:
            item["mac"] = b.shell_mac
        if b.shell_ip:
            item["ip"] = b.shell_ip
    if inactive:
        item["inactive_reason"] = _inactive_reason(b, node)
    return item


def get_port_fingerprint(session: Session, node_id: str, slot: int) -> dict:
    """单端口指纹查询；无指纹时 fingerprint/fp_verdict 为 null。"""
    binding = (session.query(WireLogPortBinding)
               .filter_by(node_id=node_id, slot=int(slot)).one_or_none())
    if binding is None:
        raise DomainError("COMRES_PORT_NOT_FOUND",
                          f"端口不存在: {node_id}/{slot}")
    fp = _parse_fingerprint_json(binding.fingerprint_json)
    return {
        "ok": True,
        "node_id": node_id,
        "slot": int(slot),
        "com": binding.com_name,
        "fingerprint": fp,
        "fp_verdict": fp.get("verdict") if isinstance(fp, dict) else None,
    }


def _classify_reach_error(exc: BaseException) -> str:
    """把 socket 异常归一为 reach_error 短码。"""
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, TimeoutError) or isinstance(exc, socket.timeout):
        return "timeout"
    en = getattr(exc, "errno", None)
    if en == errno.ECONNREFUSED:
        return "refused"
    if en in (errno.EHOSTUNREACH, errno.ENETUNREACH):
        return "no_route"
    if en == errno.ETIMEDOUT:
        return "timeout"
    if en is not None:
        return f"error:{en}"
    return f"error:{type(exc).__name__}"


def _tcp_probe(endpoint: str, timeout: float) -> tuple[bool, str | None]:
    """对 tcp://host:port 做 create_connection；返回 (ok, error_code)。

    不可达主机 ICMP 常被限速，单次易 TimeoutError；若首次为 timeout
    再试一次同预算，优先落到 no_route/refused。
    """
    try:
        host, port = parse_endpoint(endpoint)
    except SerialConfigError:
        return False, "error:bad_endpoint"
    budget = max(0.2, float(timeout))
    last_err = "timeout"
    for _attempt in range(2):
        try:
            with socket.create_connection((host, int(port)), timeout=budget):
                return True, None
        except Exception as exc:  # noqa: BLE001 - 归一后写库
            last_err = _classify_reach_error(exc)
            if last_err != "timeout":
                return False, last_err
    return False, last_err


def probe_reach(session: Session, node_id: str | None = None,
                slot: int | None = None, timeout: float = 1.5) -> dict:
    """TRES→endpoint TCP 可达性探测，写 wl_port_binding.reach_*。

    - 未限定 node_id/slot：只扫 status=online 节点上有 endpoint 的端口
    - 限定 node_id/slot：按条件扫（打开串口前预检）
    返回 ``{checked, ok, failed:[{node_id,slot,com,endpoint,error}]}``。
    """
    q = session.query(WireLogPortBinding)
    if node_id:
        q = q.filter(WireLogPortBinding.node_id == str(node_id))
    if slot is not None:
        q = q.filter(WireLogPortBinding.slot == int(slot))
    if not node_id and slot is None:
        online_ids = [
            n.node_id for n in session.query(WireLogNode)
            .filter(WireLogNode.status == "online").all()
        ]
        if not online_ids:
            return {"checked": 0, "ok": 0, "failed": []}
        q = q.filter(WireLogPortBinding.node_id.in_(online_ids))

    rows = [b for b in q.order_by(
        WireLogPortBinding.node_id, WireLogPortBinding.slot).all()
        if (b.endpoint or "").strip()]
    now = utcnow()
    checked = 0
    ok_n = 0
    failed: list[dict] = []
    for b in rows:
        ep = (b.endpoint or "").strip()
        ok, err = _tcp_probe(ep, timeout)
        b.reach_ok = bool(ok)
        b.reach_error = None if ok else (err or "error")
        b.reach_checked_at = now
        checked += 1
        if ok:
            ok_n += 1
        else:
            failed.append({
                "node_id": b.node_id,
                "slot": b.slot,
                "com": b.com_name,
                "endpoint": ep,
                "error": b.reach_error,
            })
    if checked:
        session.commit()
    return {"checked": checked, "ok": ok_n, "failed": failed}


def list_resources(session: Session) -> dict:
    """资源视图三态：已分配 / 已绑定未分配 / 纯池 + inactive（灰显）。

    F6：``auto:*`` 占位计入 bound，不计入 allocated。
    在线视图契约：health_state=offline/stale/superseded 的端口从
    allocated/bound/pool 剔除，放入 inactive（绑定关系仍保留；再次上报可恢复）。

    可分配池仅含已授权节点端口：pending/rejected/revoked 不入池
    （存量无申请记录的节点视为已授权，兼容开放登记）。
    """
    from app.domain import comres_apply

    expire_allocations(session)
    nodes = {n.node_id: n for n in session.query(WireLogNode).all()}
    auth_map = comres_apply.auth_status_by_node(session)
    apply_rows = comres_apply.auth_rows_by_node(session)
    rows = (session.query(WireLogPortBinding)
            .order_by(WireLogPortBinding.node_id, WireLogPortBinding.slot)
            .all())
    now = utcnow()
    allocated, bound, pool, inactive = [], [], [], []
    for b in rows:
        node = nodes.get(b.node_id)
        if node is not None:
            auth_status, auth_source = comres_apply.resolve_node_auth(
                node, apply_rows.get(b.node_id))
        else:
            auth_status = auth_map.get(b.node_id) or "none"
            auth_source = "none" if auth_status == "none" else "apply"
        if not _is_online_binding(b):
            item = _resource_item(
                b, node, allocated=False, bound=_is_bound(b, now),
                include_device=True, inactive=True, now=now)
            item["auth_status"] = auth_status
            item["auth_source"] = auth_source
            inactive.append(item)
            continue
        is_alloc = _is_allocated(b, now)
        is_bound = _is_bound(b, now)
        if is_alloc:
            item = _resource_item(
                b, node, allocated=True, bound=True,
                include_device=True, now=now)
            item["auth_status"] = auth_status
            item["auth_source"] = auth_source
            allocated.append(item)
        elif is_bound:
            item = _resource_item(
                b, node, allocated=False, bound=True,
                include_device=True, now=now)
            item["auth_status"] = auth_status
            item["auth_source"] = auth_source
            bound.append(item)
        else:
            # 未授权节点端口不出现在可分配池（避免误领）
            if not comres_apply.is_node_pool_authorized(b.node_id, auth_map):
                continue
            item = _resource_item(
                b, node, allocated=False, bound=False,
                include_device=False, now=now)
            item["auth_status"] = auth_status
            item["auth_source"] = auth_source
            pool.append(item)
    all_items = allocated + bound + pool + inactive
    reach_total = len(all_items)
    reach_ok_n = sum(1 for x in all_items if x.get("reach_ok") is True)
    reach_fail_n = sum(1 for x in all_items if x.get("reach_ok") is False)
    return {
        "allocated": allocated, "bound": bound, "pool": pool,
        "inactive": inactive,
        "reach_summary": {
            "total": reach_total, "ok": reach_ok_n, "fail": reach_fail_n,
        },
    }


def console_url(session: Session, node_id: str, slot: int | None = None) -> dict:
    """节点控制台地址（含 token 片段）。

    浏览器点开即可用，无需在页面上手工填 token：
    token 放在 URL fragment（``#t=``）里，不会进服务端日志/Referer，
    页面读取后写入 localStorage 并清掉 hash。
    """
    node = session.get(WireLogNode, node_id)
    if node is None:
        raise DomainError("NODE_NOT_FOUND", f"节点不存在: {node_id}")
    host = node.ip
    if not host:
        raise DomainError("NODE_NO_IP", f"节点 {node_id} 还没有上报 IP")
    cfg = node.config_json or {}
    http_port = int(cfg.get("http_port") or 8787)
    token = (node.caps_json or {}).get("local_token")  # 节点上报的 serialhub 本地 token
    if not token and getattr(node, "token_cipher", None):
        try:
            from app.secret_crypto import decrypt_secret

            token = decrypt_secret(node.token_cipher)
        except Exception:  # noqa: BLE001 - 解密失败就退回无 token 地址
            token = None
    url = f"http://{host}:{http_port}/"
    if token:
        url += f"#t={token}"
    return {"node_id": node_id, "url": url, "has_token": bool(token),
            "slot": slot}
