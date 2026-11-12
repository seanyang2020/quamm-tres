"""WireLog 域：日志机（serialhub）注册、心跳、配置、写授权与 MAC 闭环。

对应 WIRELOG-DESIGN.md。与 domain/serial.py（经 tres-logs 操作被测设备串口）
是相邻但不同的域：本模块管理「日志机本身」。
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import secrets
import time

from sqlalchemy.orm import Session

from app.models import (
    Device,
    WireLogAudit,
    WireLogEnrollment,
    WireLogNode,
    WireLogPortBinding,
)
from app.utils import hash_token, new_id, new_device_id, new_token, token_prefix, utcnow
from app.domain.catalog import DomainError
from app.constants import DeviceStatus, HealthLevel, RegisterChannel

HEARTBEAT_INTERVAL_S = 60
STALE_AFTER_S = 180
UBOOT_TOKEN_VAR = "wl_node_token"
UBOOT_ID_VAR = "wl_node_id"
UBOOT_HMAC_VAR = "wl_hmac_key"
# 局域网，时间窗取大（用户拍板）；单位秒
HMAC_WINDOW_S = 600


# ------------------------------------------------------------- 审计

def _audit(session, node_id, action, actor=None, result="ok", detail=None):
    session.add(WireLogAudit(
        node_id=node_id, action=action, actor=actor, result=result,
        detail=json.dumps(detail, ensure_ascii=False) if detail is not None else None,
        created_at=utcnow(),
    ))


# ------------------------------------------------------------- 发放（R-2）

def _find_node_by_adb_address(session, adb_address):
    """按最近一次发放记录的 adb 地址找节点（ADR-7 复用判断）。"""
    if not adb_address:
        return None
    rec = (session.query(WireLogEnrollment)
           .filter_by(adb_address=adb_address)
           .order_by(WireLogEnrollment.id.desc()).first())
    if rec is None:
        return None
    return session.get(WireLogNode, rec.node_id)


def _fw_setenv_cmds(plain, node_id, hmac_key):
    return [
        f"fw_setenv -l /tmp {UBOOT_TOKEN_VAR} {plain}",
        f"fw_setenv -l /tmp {UBOOT_ID_VAR} {node_id}",
        f"fw_setenv -l /tmp {UBOOT_HMAC_VAR} {hmac_key}",
    ]


def enroll(session, adb_address=None, operator=None, display_name=None,
           model_class="esl2_hk", reuse_existing=True):
    """生成 node_id + 明文 token，建节点与发放记录，返回写 uboot-env 所需信息。

    明文 token 只在返回值出现一次，库内只存哈希。ADR-7：同 adb 地址已有
    未退役节点时复用该节点重发 token，不再新建。
    """
    now = utcnow()
    existing = (_find_node_by_adb_address(session, adb_address)
                if reuse_existing else None)

    if existing is not None and existing.status != "retired":
        node = existing
        node_id = node.node_id
        plain = new_token()
        hmac_key = secrets.token_hex(16)
        node.node_token_hash = hash_token(plain)
        node.token_prefix = token_prefix(plain)
        node.hmac_key = hmac_key
        if display_name:
            node.display_name = display_name
        if model_class:
            node.model_class = model_class
        reused = True
    else:
        node_id = new_id("wl", 8)
        plain = new_token()
        hmac_key = secrets.token_hex(16)
        node = WireLogNode(
            node_id=node_id,
            display_name=display_name or node_id,
            model_class=model_class,
            node_token_hash=hash_token(plain),
            token_prefix=token_prefix(plain),
            hmac_key=hmac_key,
            status="enrolled",
            provisioned_at=now,
            created_at=now,
        )
        session.add(node)
        session.flush()  # 先落父行，满足 wl_enrollment 外键
        reused = False

    session.add(WireLogEnrollment(
        node_id=node_id, adb_address=adb_address, operator=operator,
        uboot_var=UBOOT_TOKEN_VAR, result="pending",
        detail=("复用节点重发 token" if reused else "等待 adb fw_setenv 写入"),
        created_at=now,
    ))
    _audit(session, node_id, "enroll", actor=operator, result="pending",
           detail={"reused": reused})
    session.commit()
    return {
        "node_id": node_id,
        "node_token": plain,                 # 一次性明文
        "hmac_key": hmac_key,               # 经 adb 写 uboot-env
        "reused": reused,
        "fw_setenv": _fw_setenv_cmds(plain, node_id, hmac_key),
    }


def retire_node(session, node_id, actor=None):
    """软退役（幂等）。在线/在用节点也允许标记，后续心跳将被拒。"""
    node = session.get(WireLogNode, node_id)
    if node is None:
        raise DomainError("WL_NODE_NOT_FOUND", "日志机不存在")
    node.status = "retired"
    _audit(session, node_id, "retire", actor=actor)
    session.commit()
    return {"node_id": node_id, "status": "retired"}


def delete_node(session, node_id, actor=None):
    """硬删 retired 节点：级联清除其全部绑定/审计/发放引用后删除节点。"""
    node = session.get(WireLogNode, node_id)
    if node is None:
        raise DomainError("WL_NODE_NOT_FOUND", "日志机不存在")
    if node.status != "retired":
        raise DomainError("WL_NOT_RETIRED", "仅 retired 节点可硬删，请先退役")
    counts = {
        "bindings": session.query(WireLogPortBinding)
                    .filter_by(node_id=node_id).delete(synchronize_session=False),
        "audits": session.query(WireLogAudit)
                  .filter_by(node_id=node_id).delete(synchronize_session=False),
        "enrollments": session.query(WireLogEnrollment)
                      .filter_by(node_id=node_id).delete(synchronize_session=False),
    }
    session.delete(node)
    session.commit()
    return {"node_id": node_id, "deleted": True, "cascade": counts}


def uart_register(session, node_id, slot, actor=None,
                  position_code=None, owner=None):
    """经 WireLog 串口全量探测并注册/更新设备（ADR-8/9）。

    门槛：MAC 与 SN 二选一。命中已有设备（按 MAC/SN 锚定）则更新并绑定，
    否则新建，register_channel=wirelog_uart。
    """
    from app.domain.prober_uart import UartProber

    node = session.get(WireLogNode, node_id)
    if node is None:
        raise DomainError("WL_NODE_NOT_FOUND", "日志机不存在")
    caps = node.caps_json if isinstance(node.caps_json, dict) else {}
    rport = int(caps.get("raw_tcp_port") or 9001)
    token = caps.get("local_token") or ""
    res = UartProber().probe(node.ip, rport, token=token, full=True)
    if not res.reachable_l3 or (res.mac is None and res.sn is None):
        raise DomainError("WL_UART_NO_IDENTITY",
                          "uart 未取到 MAC/SN，无法注册",
                          {"extra": res.extra})
    shell_ip = (res.extra or {}).get("shell_ip")
    now = utcnow()

    existing = None
    if res.mac:
        existing = session.query(Device).filter_by(mac=res.mac).first()
    if existing is None and res.sn:
        existing = session.query(Device).filter_by(sn=res.sn).first()

    if existing is not None:
        device = existing
        if res.mac:
            device.mac = res.mac
        if res.sn:
            device.sn = res.sn
        if shell_ip:
            device.ip = shell_ip
        matched = "existing"
    else:
        device = Device(
            device_id=new_device_id(),
            display_name=f"uart-{res.sn or res.mac}",
            mac=res.mac, sn=res.sn,
            position_code=position_code, owner=owner or actor or "system",
            status=DeviceStatus.PENDING, health_level=HealthLevel.L3,
            registered_by=actor,
            register_channel=RegisterChannel.WIRELOG_UART,
            source="real", ip=shell_ip,
            last_seen_at=now, created_at=now,
        )
        session.add(device)
        session.flush()
        matched = "new"

    binding = _get_or_create_binding(session, node_id, int(slot))
    binding.bound_device_id = device.device_id
    binding.shell_reachable = True
    if res.mac:
        binding.shell_mac = res.mac
    if shell_ip:
        binding.shell_ip = shell_ip
    binding.last_checked_at = now
    _audit(session, node_id, "uart_register", actor=actor,
           detail={"device": device.device_id, "matched": matched})
    session.commit()
    return {"device_id": device.device_id, "matched": matched,
            "mac": res.mac, "sn": res.sn, "shell_ip": shell_ip}


def _get_or_create_binding(session, node_id, slot):
    binding = (session.query(WireLogPortBinding)
               .filter_by(node_id=node_id, slot=slot).one_or_none())
    if binding is None:
        binding = WireLogPortBinding(node_id=node_id, slot=slot)
        session.add(binding)
    return binding


def reconcile_identity(session, node_id, slot, observed, actor=None):
    """全量探测后按 MAC/SN 锚定身份，处理 IP 漂移与物理串口换位（ADR-9）。

    observed: dict(mac, sn, ip, reachable)。
    """
    binding = _get_or_create_binding(session, node_id, int(slot))
    now = utcnow()
    binding.shell_reachable = bool(observed.get("reachable"))
    binding.last_checked_at = now
    device_id = binding.bound_device_id
    if device_id is None:
        if observed.get("mac"):
            binding.shell_mac = observed["mac"]
        if observed.get("ip"):
            binding.shell_ip = observed["ip"]
        session.commit()
        return {"action": "unbound", "reason": "no anchored device"}

    device = session.get(Device, device_id)
    if device is None:
        binding.bound_device_id = None
        session.commit()
        return {"action": "freed", "reason": "anchored device missing"}

    mac = observed.get("mac")
    sn = observed.get("sn")
    ip = observed.get("ip")
    same_mac = mac and device.mac == mac
    same_sn = sn and device.sn == sn

    # MAC 与 SN 都对不上 → 物理串口换位，该路转自由串口
    if not same_mac and not same_sn:
        binding.bound_device_id = None
        binding.shell_mac = None
        binding.shell_ip = None
        binding.write_granted_to = None
        _audit(session, node_id, "port_freed", actor=actor,
               detail={"slot": slot, "old_device": device_id,
                       "observed_mac": mac})
        session.commit()
        return {"action": "freed", "reason": "identity changed"}

    if mac and not same_mac:
        device.mac = mac
    if sn and not same_sn:
        device.sn = sn
    binding.shell_mac = mac or binding.shell_mac
    if ip and device.ip != ip:
        old_ip = device.ip
        device.ip = ip
        binding.shell_ip = ip
        _audit(session, node_id, "ip_drift", actor=actor,
               detail={"device": device_id, "old_ip": old_ip, "new_ip": ip})
    elif ip:
        binding.shell_ip = ip
    session.commit()
    return {"action": "anchored", "device_id": device_id}


def _completeness(device):
    fields = ("mac", "sn", "ip", "model_class", "display_name",
              "position_code", "owner", "android_version", "build_fingerprint")
    return sum(1 for f in fields if getattr(device, f, None))


def merge_mac_conflicts(session, actor=None):
    """同 MAC 多设备归并：按是否有活动租约决定删/留（ADR-9）。"""
    from app.models import Lease, LeaseState
    from app.domain.patrol import _has_active_lease

    merged = []
    rows = session.query(Device).filter(Device.mac.isnot(None)).all()
    groups = {}
    for d in rows:
        groups.setdefault(d.mac, []).append(d)
    for mac, devices in groups.items():
        if len(devices) < 2:
            continue
        occupied = [d for d in devices if _has_active_lease(session, d.device_id)]
        if occupied:
            keep = occupied[0]
            drop = [d for d in devices if d.device_id != keep.device_id]
        else:
            devices_sorted = sorted(devices, key=_completeness, reverse=True)
            keep = devices_sorted[0]
            drop = devices_sorted[1:]
        for d in drop:
            (session.query(WireLogPortBinding)
             .filter_by(bound_device_id=d.device_id)
             .update({"bound_device_id": None}, synchronize_session=False))
            _audit(session, None, "mac_merge", actor=actor,
                   detail={"mac": mac, "kept": keep.device_id,
                           "dropped": d.device_id,
                           "reason": "occupied" if occupied else "less_complete"})
            session.delete(d)
        merged.append({"mac": mac, "kept": keep.device_id,
                       "dropped": [d.device_id for d in drop]})
    session.commit()
    return {"merged": merged}


def mark_enroll_result(session, node_id, result, detail=None):
    rec = (session.query(WireLogEnrollment)
           .filter_by(node_id=node_id).order_by(WireLogEnrollment.id.desc()).first())
    if rec:
        rec.result = result
        if detail:
            rec.detail = detail
    _audit(session, node_id, "enroll_result", result=result, detail={"detail": detail})
    session.commit()


# ------------------------------------------------------------- 鉴权

def _auth_node(session, token):
    if not token:
        raise DomainError("WL_UNAUTHORIZED", "缺少 node token")
    node = (session.query(WireLogNode)
            .filter_by(node_token_hash=hash_token(token)).one_or_none())
    if node is None:
        raise DomainError("WL_UNAUTHORIZED", "node token 无效")
    if node.status == "retired":
        raise DomainError("WL_NODE_RETIRED", "日志机已退役")
    return node


# ------------------------------------------------------------- 反向签名（ADR-6）

def sign_response(session, token, nonce, data):
    """对返回节点的 body 做 HMAC-SHA256，签名串: ts|nonce|body。

    返回 (compact_body, headers)；设备侧用 wlhmac 校验时间窗与签名。
    """
    node = _auth_node(session, token)
    if not node.hmac_key:
        raise DomainError("WL_NO_HMAC_KEY", "节点未配置 hmac key")
    ts = str(int(time.time()))
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    msg = f"{ts}|{nonce}|{body}".encode("utf-8")
    key_bytes = bytes.fromhex(node.hmac_key)  # 与设备 wlhmac 的 hex 解码一致
    sig = hmac_mod.new(key_bytes, msg, hashlib.sha256).hexdigest()
    return body, {"X-WL-Ts": ts, "X-WL-Nonce": nonce, "X-WL-Sig": sig}


def _upsert_binding(session, node, port):
    slot = int(port.get("slot", 0))
    binding = (session.query(WireLogPortBinding)
               .filter_by(node_id=node.node_id, slot=slot).one_or_none())
    if binding is None:
        binding = WireLogPortBinding(node_id=node.node_id, slot=slot)
        session.add(binding)
    binding.tty_name = port.get("tty")
    binding.baud = int(port.get("baud", 115200))
    # 去抖：节点在线时单次串口探测失败不立即降级（串口读失败≠shell 宕机），
    # 保留上一次成功状态；节点离线（心跳超时）时状态机另行判定。
    new_reachable = bool(port.get("shell_reachable", False))
    if not new_reachable and binding.shell_reachable and node.status == "online":
        pass
    else:
        binding.shell_reachable = new_reachable
    if port.get("shell_mac"):
        binding.shell_mac = port["shell_mac"]
    if port.get("shell_ip"):
        binding.shell_ip = port["shell_ip"]
    binding.last_checked_at = utcnow()
    return binding


# ------------------------------------------------------------- 注册/心跳

def register(session, token, payload):
    node = _auth_node(session, token)
    now = utcnow()
    node.ip = payload.get("ip")
    node.sw_version = payload.get("sw_version")
    node.caps_json = payload.get("caps")
    node.last_heartbeat_at = now
    node.status = "online"
    for port in payload.get("ports", []):
        binding = _upsert_binding(session, node, port)
        _maybe_auto_bind(session, binding, port)
    _audit(session, node.node_id, "register", actor=node.ip,
           detail={"ports": len(payload.get("ports", []))})
    session.commit()
    return {
        "node_id": node.node_id,
        "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
        "config": node.config_json or {},
        "write_policy": _write_policy(session, node),
    }


def heartbeat(session, token, payload):
    node = _auth_node(session, token)
    node.last_heartbeat_at = utcnow()
    node.status = "online"
    if payload.get("ip"):
        node.ip = payload["ip"]
    for port in payload.get("ports", []):
        binding = _upsert_binding(session, node, port)
        _maybe_auto_bind(session, binding, port)
    session.commit()
    return {
        "node_id": node.node_id,
        "config": node.config_json or {},
        "write_policy": _write_policy(session, node),
    }


def report_event(session, token, event):
    node = _auth_node(session, token)
    _audit(session, node.node_id, event.get("action", "event"),
           actor=node.ip, result=event.get("result"), detail=event)
    session.commit()
    return {"ok": True}


# ------------------------------------------------------------- MAC 闭环（R-3）

def _maybe_auto_bind(session, binding, port):
    """shell 实测 MAC 命中 device.mac 则建立绑定（未命中不动，待人工）。"""
    mac = (port.get("shell_mac") or "").strip().lower()
    if not mac or binding.bound_device_id:
        return
    device = session.query(Device).filter(Device.mac.ilike(mac)).one_or_none()
    if device is not None:
        binding.bound_device_id = device.device_id
        binding.health_state = "ok"
        _audit(session, binding.node_id, "auto_bind",
               detail={"slot": binding.slot, "device": device.device_id})


def bind_port(session, node_id, slot, device_id=None, actor=None, confirm=False):
    binding = (session.query(WireLogPortBinding)
               .filter_by(node_id=node_id, slot=int(slot)).one_or_none())
    if binding is None:
        raise DomainError("WL_BINDING_NOT_FOUND", f"路 {slot} 不存在")
    if device_id is None:
        if not binding.shell_mac:
            raise DomainError("WL_NO_MAC", "该路无 shell MAC，无法自动匹配")
        device = session.query(Device).filter(
            Device.mac.ilike(binding.shell_mac.lower())).one_or_none()
        if device is None:
            raise DomainError("WL_DEVICE_NOT_FOUND", "MAC 未匹配到注册设备")
        device_id = device.device_id
    if session.get(Device, device_id) is None:
        raise DomainError("WL_DEVICE_NOT_FOUND", "目标设备不存在")
    binding.bound_device_id = device_id
    _audit(session, node_id, "bind_port", actor=actor,
           detail={"slot": slot, "device": device_id})
    session.commit()
    return {"node_id": node_id, "slot": slot, "device_id": device_id}


# ------------------------------------------------------------- 写授权（R-3）

def _write_policy(session, node):
    rows = (session.query(WireLogPortBinding)
            .filter_by(node_id=node.node_id).all())
    return {
        str(r.slot): {"write_granted_to": r.write_granted_to,
                      "shell_reachable": r.shell_reachable,
                      "bound_device_id": r.bound_device_id}
        for r in rows
    }


def grant_write(session, node_id, slot, principal, actor=None):
    binding = _get_binding_or_raise(session, node_id, slot)
    binding.write_granted_to = principal
    _audit(session, node_id, "grant_write", actor=actor,
           detail={"slot": slot, "to": principal})
    session.commit()
    return _write_policy(session, session.get(WireLogNode, node_id))


def revoke_write(session, node_id, slot, actor=None):
    binding = _get_binding_or_raise(session, node_id, slot)
    binding.write_granted_to = None
    _audit(session, node_id, "revoke_write", actor=actor, detail={"slot": slot})
    session.commit()
    return _write_policy(session, session.get(WireLogNode, node_id))


def _get_binding_or_raise(session, node_id, slot):
    binding = (session.query(WireLogPortBinding)
               .filter_by(node_id=node_id, slot=int(slot)).one_or_none())
    if binding is None:
        raise DomainError("WL_BINDING_NOT_FOUND", f"路 {slot} 不存在")
    return binding


# ------------------------------------------------------------- 配置下发

def set_config(session, node_id, config, actor=None):
    node = session.get(WireLogNode, node_id)
    if node is None:
        raise DomainError("WL_NODE_NOT_FOUND", "日志机不存在")
    node.config_json = config
    _audit(session, node_id, "set_config", actor=actor, detail=config)
    session.commit()
    return {"node_id": node_id, "config": node.config_json}


# ------------------------------------------------------------- 查询

def list_nodes(session):
    nodes = session.query(WireLogNode).all()
    return [_node_brief(session, n) for n in nodes]


def get_node(session, node_id):
    node = session.get(WireLogNode, node_id)
    if node is None:
        raise DomainError("WL_NODE_NOT_FOUND", "日志机不存在")
    bindings = (session.query(WireLogPortBinding)
                .filter_by(node_id=node_id).all())
    return {
        **_node_brief(session, node),
        "caps": public_caps(node),
        "config": node.config_json,
        "ports": [
            {"slot": b.slot, "tty": b.tty_name, "baud": b.baud,
             "shell_reachable": b.shell_reachable, "shell_mac": b.shell_mac,
             "shell_ip": b.shell_ip, "bound_device_id": b.bound_device_id,
             "bound_device": _device_summary(session, b.bound_device_id),
             "write_granted_to": b.write_granted_to,
             "health_state": b.health_state}
            for b in bindings
        ],
    }


def _device_summary(session, device_id):
    if not device_id:
        return None
    d = session.get(Device, device_id)
    if d is None:
        return None
    return {
        "device_id": d.device_id, "name": d.display_name,
        "status": d.status, "health_level": d.health_level,
        "mac": d.mac, "ip": d.ip, "sn": d.sn,
    }


def public_caps(node) -> dict:
    """对外展示用的 caps：**剥掉 local_token（主 token）**。

    主 token 是本机管理通道凭据（TRES 用它写派生票），任何 agent/Web 用户都不该拿到；
    只暴露「有没有配」这一位布尔，方便排障区分"未上报"和"上报了空值"。
    """
    caps = node.caps_json if isinstance(node.caps_json, dict) else {}
    if isinstance(node.caps_json, str):
        import json as _json
        try:
            parsed = _json.loads(node.caps_json)
            caps = parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            caps = {}
    safe = {k: v for k, v in caps.items() if k != "local_token"}
    safe["local_token_present"] = bool(str(caps.get("local_token") or "").strip())
    return safe


def _node_brief(session, node):
    return {
        "node_id": node.node_id, "display_name": node.display_name,
        "model_class": node.model_class, "status": node.status,
        "ip": node.ip, "sw_version": node.sw_version,
        "token_prefix": node.token_prefix,
        "last_heartbeat_at": node.last_heartbeat_at.isoformat()
        if node.last_heartbeat_at else None,
    }


def device_bindings(session, device_id):
    """返回某设备当前经 WireLog 绑定的全部串口（join 节点信息，不加表）。"""
    rows = (session.query(WireLogPortBinding)
            .filter_by(bound_device_id=device_id).all())
    out = []
    for b in rows:
        node = session.get(WireLogNode, b.node_id)
        out.append({
            "node_id": b.node_id,
            "node_display": node.display_name if node else None,
            "node_status": node.status if node else None,
            "node_ip": node.ip if node else None,
            "slot": b.slot, "tty": b.tty_name, "baud": b.baud,
            "shell_reachable": b.shell_reachable,
            "shell_mac": b.shell_mac, "shell_ip": b.shell_ip,
            "write_granted_to": b.write_granted_to,
            "health_state": b.health_state,
        })
    return out


def audit_tail(session, node_id=None, limit=50):
    q = session.query(WireLogAudit)
    if node_id:
        q = q.filter_by(node_id=node_id)
    rows = q.order_by(WireLogAudit.id.desc()).limit(int(limit)).all()
    return [
        {"id": r.id, "node_id": r.node_id, "action": r.action,
         "actor": r.actor, "result": r.result, "detail": r.detail,
         "created_at": r.created_at.isoformat() if r.created_at else None}
        for r in reversed(rows)
    ]


def refresh_status(session):
    """按心跳时间把超时节点标 stale/offline（可由巡检周期调用）。"""
    now = utcnow()
    for node in session.query(WireLogNode).filter(WireLogNode.status == "online"):
        if not node.last_heartbeat_at:
            continue
        age = (now - node.last_heartbeat_at).total_seconds()
        if age > STALE_AFTER_S:
            node.status = "offline"
        elif age > HEARTBEAT_INTERVAL_S:
            node.status = "stale"
    session.commit()
