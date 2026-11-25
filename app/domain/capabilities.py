"""设备统一能力发现（Phase 1）：只做 discovery，不扩大执行面。

主入口：
- build_snapshot(session, device_id) → res_device_info.capabilities
- build_summaries(session, device_ids) → res_list_devices.capabilities_summary

禁止在本模块调用 bundle.build_bundles / device_bundle。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from app.constants import DeviceStatus
from app.models import (
    Device,
    DeviceSerialPort,
    SerialAction,
    SerialState,
    Setting,
    WireLogNode,
    WireLogPortBinding,
)
from app.utils import utcnow

# ---- 状态 / 原因常量 -------------------------------------------------------

STATUS_AVAILABLE = "available"
STATUS_DEGRADED = "degraded"
STATUS_UNAVAILABLE = "unavailable"
STATUS_UNKNOWN = "unknown"

REASON_NO_BINDING = "NO_BINDING"
REASON_NO_ACTION_MODEL = "NO_ACTION_MODEL"
REASON_ENDPOINT_OFFLINE = "ENDPOINT_OFFLINE"
REASON_ENDPOINT_OPEN_FAILED = "ENDPOINT_OPEN_FAILED"
REASON_ENDPOINT_UNREACHABLE = "ENDPOINT_UNREACHABLE"
REASON_HEALTH_SUSPECT = "HEALTH_SUSPECT"
REASON_NO_ENDPOINT = "NO_ENDPOINT"
REASON_DEVICE_OFFLINE = "DEVICE_OFFLINE"
REASON_DEVICE_RETIRED = "DEVICE_RETIRED"
REASON_SHARED_ENDPOINT = "SHARED_ENDPOINT"
REASON_DISABLED = "DISABLED"
REASON_PARTIAL_ACTIONS = "PARTIAL_ACTIONS"
REASON_NOT_PROBED = "NOT_PROBED"
REASON_NO_CAPABILITY_PROVIDER = "NO_CAPABILITY_PROVIDER"

KIND_ADB = "adb"
KIND_SERIAL_IO = "serial.io"
KIND_POWER = "power"
KIND_MODE = "mode"

SERIAL_KINDS = frozenset({KIND_SERIAL_IO, KIND_POWER, KIND_MODE})

POWER_KEYS = ("power_on", "power_off", "power_cycle")
MODE_KEYS = ("mode_normal", "mode_fastboot", "mode_edl")

_FLAG_KEY = "capabilities_v1"
# 明确可用（节点/探测自报）
_OK_HEALTH = frozenset({"online", "ok", "listen_ok", "probed"})
# 端点不可用 / 已失效
_OFFLINE_HEALTH = frozenset({
    "offline", "no_listen", "stale", "superseded", "open_failed",
})
# 可疑但仍可能部分可用
_SUSPECT_HEALTH = frozenset({"fail", "suspect", "no_response"})

# health 保守序：越大越差
_HEALTH_RANK = {
    "online": 0,
    "ok": 0,
    "listen_ok": 0,
    "probed": 0,
    "unknown": 1,
    "suspect": 2,
    "fail": 3,
    "no_response": 3,
    "no_listen": 4,
    "open_failed": 4,
    "stale": 5,
    "superseded": 5,
    "offline": 5,
}


def is_enabled(session: Session) -> bool:
    """capabilities_v1 开关；默认 true。直接读 Setting，避免改 runtime_config 白名单。"""
    row = session.get(Setting, _FLAG_KEY)
    if row is None or row.value is None or row.value == "":
        return True
    return str(row.value).strip().lower() in ("1", "true", "yes", "on")


# ---- 公共入口 --------------------------------------------------------------

def build_snapshot(session: Session, device_id: str) -> dict:
    """单设备完整能力快照；用于 res_device_info / res_serial_caps 增强。"""
    ctx = _load_context(session, [device_id])
    device = ctx["devices"].get(device_id)
    if device is None:
        from app.domain.catalog import DomainError

        raise DomainError("NOT_FOUND", f"device {device_id} not found")
    items = _items_for_device(ctx, device)
    revision = _revision(items)
    # 对外不带 steps 明文（revision 已吃过 steps_sha）
    public_items = [_public_item(i) for i in items]
    summary = _summary_from_items(public_items)
    updated = utcnow().isoformat()
    return {
        "revision": revision,
        "updated_at": updated,
        "items": public_items,
        "summary": summary,
    }


def build_summaries(session: Session, device_ids: list[str]) -> dict[str, dict]:
    """批量能力摘要；用于 res_list_devices。禁止逐设备 build_snapshot。"""
    ids = [d for d in device_ids if d]
    if not ids:
        return {}
    ctx = _load_context(session, ids)
    out: dict[str, dict] = {}
    for did in ids:
        device = ctx["devices"].get(did)
        if device is None:
            continue
        items = _items_for_device(ctx, device)
        revision = _revision(items)
        public_items = [_public_item(i) for i in items]
        summary = _summary_from_items(public_items)
        out[did] = {
            "revision": revision,
            "adb": summary["adb"],
            "serial": summary["serial"],
            "power": summary["power"],
            "mode": summary["mode"],
            "counts": summary["counts"],
        }
    return out


def observed_channels(session: Session, device_id: str) -> list[dict]:
    """从 WireLogPortBinding 派生发现视图；字段与 serial_channels 对齐。"""
    ctx = _load_context(session, [device_id])
    return [
        _observed_channel_dict(b, ctx["nodes"].get(b.node_id))
        for b in ctx["bindings"].get(device_id, [])
    ]


def enrich_declared_port(
    port: dict,
    *,
    items: list[dict],
    bindings: list[WireLogPortBinding],
    nodes: dict[str, WireLogNode],
    shared_keys: set[str],
) -> dict:
    """给 caps().ports[] 里的 declared port 补 capability_id/source/binding/constraints。"""
    slot = port.get("slot")
    role = port.get("role") or "spare"
    if role == "power":
        cap_id = f"power:slot{slot}"
    elif role == "mode":
        cap_id = f"mode:slot{slot}"
    else:
        cap_id = f"serial.io:slot{slot}"
    item = next((i for i in items if i.get("id") == cap_id), None)
    if item is None:
        # 回退：同 slot 的 serial.io
        item = next(
            (i for i in items
             if i.get("kind") == KIND_SERIAL_IO and i.get("id") == f"serial.io:slot{slot}"),
            None,
        )
    binding_obj = _match_binding_to_declared(port, bindings)
    binding_view = (
        item.get("binding") if item and item.get("binding")
        else (_binding_view(binding_obj, nodes.get(binding_obj.node_id), shared_keys)
              if binding_obj else None)
    )
    constraints = (
        item.get("constraints") if item
        else _default_constraints(read_only=(role in ("log", "spare")), shared=False)
    )
    out = dict(port)
    out["capability_id"] = cap_id
    out["source"] = (item.get("source") if item else "declared")
    out["binding"] = binding_view
    out["constraints"] = constraints
    return out


# ---- 批量加载 --------------------------------------------------------------

def _load_context(session: Session, device_ids: list[str]) -> dict[str, Any]:
    """一次批量查 Device / Binding / Port / Action / State / Node。"""
    devices = {
        d.device_id: d
        for d in session.query(Device).filter(Device.device_id.in_(device_ids)).all()
    }
    bindings_rows = (
        session.query(WireLogPortBinding)
        .filter(WireLogPortBinding.bound_device_id.in_(device_ids))
        .all()
    )
    bindings: dict[str, list[WireLogPortBinding]] = {did: [] for did in device_ids}
    for b in bindings_rows:
        if b.bound_device_id:
            bindings.setdefault(b.bound_device_id, []).append(b)

    ports_rows = (
        session.query(DeviceSerialPort)
        .filter(DeviceSerialPort.device_id.in_(device_ids))
        .order_by(DeviceSerialPort.slot)
        .all()
    )
    ports: dict[str, list[DeviceSerialPort]] = {did: [] for did in device_ids}
    for p in ports_rows:
        ports.setdefault(p.device_id, []).append(p)

    port_ids = [p.id for p in ports_rows]
    actions_by_port: dict[int, list[SerialAction]] = {pid: [] for pid in port_ids}
    if port_ids:
        for a in (
            session.query(SerialAction)
            .filter(SerialAction.port_id.in_(port_ids))
            .order_by(SerialAction.sort, SerialAction.id)
            .all()
        ):
            actions_by_port.setdefault(a.port_id, []).append(a)

    states = {
        s.device_id: s
        for s in session.query(SerialState)
        .filter(SerialState.device_id.in_(device_ids))
        .all()
    }

    # shared：并入 binding 级 endpoint/com_key（全表扫描绑定键；测试库规模可接受）
    all_bound = (
        session.query(WireLogPortBinding)
        .filter(WireLogPortBinding.bound_device_id.isnot(None))
        .all()
    )
    shared_keys, shared_peers = _binding_shared_index(all_bound)

    node_ids = {b.node_id for b in bindings_rows} | {b.node_id for b in all_bound}
    nodes = {}
    if node_ids:
        nodes = {
            n.node_id: n
            for n in session.query(WireLogNode).filter(WireLogNode.node_id.in_(node_ids)).all()
        }

    return {
        "devices": devices,
        "bindings": bindings,
        "ports": ports,
        "actions_by_port": actions_by_port,
        "states": states,
        "nodes": nodes,
        "shared_keys": shared_keys,
        "shared_peers": shared_peers,
        "all_bound": all_bound,
    }


def _binding_shared_index(
    rows: list[WireLogPortBinding],
) -> tuple[set[str], dict[str, list[dict]]]:
    """同一 endpoint/com_key 绑到 ≥2 个不同 device → shared。"""
    by_key: dict[str, list[WireLogPortBinding]] = {}
    for b in rows:
        for key in _binding_share_keys(b):
            by_key.setdefault(key, []).append(b)
    shared_keys: set[str] = set()
    peers: dict[str, list[dict]] = {}
    for key, group in by_key.items():
        device_ids = {b.bound_device_id for b in group if b.bound_device_id}
        if len(device_ids) < 2:
            continue
        shared_keys.add(key)
        peers[key] = [
            {
                "device_id": b.bound_device_id,
                "slot": b.slot,
                "label": b.com_name,
                "node_id": b.node_id,
            }
            for b in group
            if b.bound_device_id
        ]
    return shared_keys, peers


def _binding_share_keys(b: WireLogPortBinding) -> list[str]:
    keys: list[str] = []
    ep = _norm_endpoint(b.endpoint)
    if ep:
        keys.append(f"ep:{ep}")
    ck = (b.com_key or "").strip().lower()
    if ck:
        keys.append(f"ck:{ck}")
    return keys


# ---- 分项收集 --------------------------------------------------------------

def _items_for_device(ctx: dict, device: Device) -> list[dict]:
    items: list[dict] = []
    items.extend(_collect_adb(device))
    items.extend(
        _serial_items(
            device,
            bindings=ctx["bindings"].get(device.device_id, []),
            ports=ctx["ports"].get(device.device_id, []),
            actions_by_port=ctx["actions_by_port"],
            nodes=ctx["nodes"],
            shared_keys=ctx["shared_keys"],
            shared_peers=ctx["shared_peers"],
        )
    )
    return items


def _collect_adb(device: Device) -> list[dict]:
    reasons: list[str] = []
    status = STATUS_AVAILABLE
    addr = device.adb_address
    if device.status == DeviceStatus.RETIRED:
        status = STATUS_UNAVAILABLE
        reasons.append(REASON_DEVICE_RETIRED)
    elif not addr:
        status = STATUS_UNAVAILABLE
        reasons.append(REASON_NO_ENDPOINT)
    elif device.status == DeviceStatus.OFFLINE:
        status = STATUS_DEGRADED
        reasons.append(REASON_DEVICE_OFFLINE)

    actions = []
    if status in (STATUS_AVAILABLE, STATUS_DEGRADED):
        actions = [{
            "action_key": "adb_shell",
            "kind": "adb",
            "enabled": True,
            "inter_step_ms": 0,
            "steps": [],
            "steps_sha256": _steps_sha([]),
        }]
    return [{
        "id": "adb",
        "kind": KIND_ADB,
        "provider": "adb",
        "label": "ADB",
        "status": status,
        "status_reasons": reasons,
        "transport": {
            "kind": "adb",
            "endpoint": addr,
            "port_name": None,
            "channel": "adb",
            "baud": None,
        } if addr else None,
        "binding": None,
        "actions": actions,
        "health": {
            "state": device.health_level or "unknown",
            "reach_ok": None,
            "error": None,
            "checked_at": (
                device.last_probe_at.isoformat() if device.last_probe_at else None
            ),
        },
        "constraints": _default_constraints(read_only=False, shared=False),
        "source": "declared",
        "detail_tool": None,
        "detail_args": None,
        "observed_at": (
            device.last_seen_at.isoformat() if device.last_seen_at else None
        ),
    }]


def _serial_items(
    device: Device,
    *,
    bindings: list[WireLogPortBinding],
    ports: list[DeviceSerialPort],
    actions_by_port: dict[int, list[SerialAction]],
    nodes: dict[str, WireLogNode],
    shared_keys: set[str],
    shared_peers: dict[str, list[dict]],
) -> list[dict]:
    items: list[dict] = []
    matched_bindings: set[int] = set()

    # 1) declared ports → serial.io (+ power/mode)
    for port in ports:
        binding = _match_binding_to_declared(
            {
                "endpoint": port.endpoint,
                "port_name": port.port_name,
                "slot": port.slot,
            },
            bindings,
        )
        if binding is not None:
            matched_bindings.add(binding.id)
        items.append(
            _build_serial_io_item(
                device,
                port=port,
                binding=binding,
                nodes=nodes,
                shared_keys=shared_keys,
                shared_peers=shared_peers,
                source="declared",
            )
        )
        if port.role == "power" and port.enabled:
            items.append(
                _build_power_or_mode_item(
                    device,
                    kind=KIND_POWER,
                    port=port,
                    binding=binding,
                    actions=actions_by_port.get(port.id, []),
                    nodes=nodes,
                    shared_keys=shared_keys,
                    shared_peers=shared_peers,
                )
            )
        elif port.role == "mode" and port.enabled:
            items.append(
                _build_power_or_mode_item(
                    device,
                    kind=KIND_MODE,
                    port=port,
                    binding=binding,
                    actions=actions_by_port.get(port.id, []),
                    nodes=nodes,
                    shared_keys=shared_keys,
                    shared_peers=shared_peers,
                )
            )

    # 2) binding-only → serial.io observed
    for binding in bindings:
        if binding.id in matched_bindings:
            continue
        items.append(
            _build_serial_io_item(
                device,
                port=None,
                binding=binding,
                nodes=nodes,
                shared_keys=shared_keys,
                shared_peers=shared_peers,
                source="observed",
            )
        )
    return items


def _build_serial_io_item(
    device: Device,
    *,
    port: DeviceSerialPort | None,
    binding: WireLogPortBinding | None,
    nodes: dict[str, WireLogNode],
    shared_keys: set[str],
    shared_peers: dict[str, list[dict]],
    source: str,
) -> dict:
    if port is not None:
        cap_id = f"serial.io:slot{port.slot}"
        label = port.label or f"slot {port.slot}"
    else:
        assert binding is not None
        key = (binding.com_key or _norm_endpoint(binding.endpoint)
               or f"{binding.node_id}:{binding.slot}")
        cap_id = f"serial.io:{key}"
        label = binding.com_name or key

    reasons: list[str] = []
    status = STATUS_AVAILABLE
    read_only = source == "observed" or (port is not None and port.role in ("log", "spare"))
    actions: list[dict] = []

    if port is not None and not port.enabled:
        status = STATUS_UNAVAILABLE
        reasons.append(REASON_DISABLED)

    if binding is None:
        status = STATUS_UNAVAILABLE
        if REASON_DISABLED not in reasons:
            reasons.append(REASON_NO_BINDING)
    else:
        st, rs = _binding_status(binding)
        if _worse(st, status) != status or st == STATUS_UNAVAILABLE:
            status = _merge_status(status, st)
        for r in rs:
            if r not in reasons:
                reasons.append(r)

    status, reasons = _apply_device_status(device, status, reasons)

    shared = False
    shared_with: list[dict] = []
    if binding is not None:
        for key in _binding_share_keys(binding):
            if key in shared_keys:
                shared = True
                for peer in shared_peers.get(key, []):
                    if peer["device_id"] != device.device_id and peer not in shared_with:
                        shared_with.append(peer)
    if shared:
        if REASON_SHARED_ENDPOINT not in reasons:
            reasons.append(REASON_SHARED_ENDPOINT)
        if status == STATUS_AVAILABLE:
            status = STATUS_DEGRADED

    # declared + healthy：可声明短读动作（仍走 declared 执行路径）
    if (
        source == "declared"
        and port is not None
        and status in (STATUS_AVAILABLE, STATUS_DEGRADED)
        and binding is not None
    ):
        actions = [{
            "action_key": "serial_read",
            "kind": "serial",
            "enabled": True,
            "inter_step_ms": 0,
            "steps": [],
            "steps_sha256": _steps_sha([]),
        }]

    node = nodes.get(binding.node_id) if binding else None
    transport = _transport_from(port, binding)
    binding_view = _binding_view(binding, node, shared_keys, shared_with=shared_with)
    health = _health_from(port, binding)
    observed_at = None
    if binding and binding.last_checked_at:
        observed_at = binding.last_checked_at.isoformat()
    elif binding and binding.reach_checked_at:
        observed_at = binding.reach_checked_at.isoformat()

    return {
        "id": cap_id,
        "kind": KIND_SERIAL_IO,
        "provider": "serial",
        "label": label,
        "status": status,
        "status_reasons": reasons,
        "transport": transport,
        "binding": binding_view,
        "actions": actions,
        "health": health,
        "constraints": _default_constraints(read_only=read_only, shared=shared),
        "source": source,
        "detail_tool": "res_serial_caps",
        "detail_args": {"device_id": device.device_id},
        "observed_at": observed_at,
    }


def _build_power_or_mode_item(
    device: Device,
    *,
    kind: str,
    port: DeviceSerialPort,
    binding: WireLogPortBinding | None,
    actions: list[SerialAction],
    nodes: dict[str, WireLogNode],
    shared_keys: set[str],
    shared_peers: dict[str, list[dict]],
) -> dict:
    cap_id = f"{kind}:slot{port.slot}"
    label = port.label or f"{kind} slot {port.slot}"
    reasons: list[str] = []
    status = STATUS_AVAILABLE

    enabled_acts = [a for a in actions if a.enabled]
    relevant = [
        a for a in enabled_acts
        if (kind == KIND_POWER and (a.action_key in POWER_KEYS or a.kind == "power"))
        or (kind == KIND_MODE and (a.action_key in MODE_KEYS or a.kind == "mode"))
    ]

    if not port.enabled:
        status = STATUS_UNAVAILABLE
        reasons.append(REASON_DISABLED)
    elif not relevant:
        status = STATUS_UNAVAILABLE
        reasons.append(REASON_NO_ACTION_MODEL)
    elif binding is None:
        status = STATUS_UNAVAILABLE
        reasons.append(REASON_NO_BINDING)
    else:
        st, rs = _binding_status(binding)
        status = _merge_status(status, st)
        for r in rs:
            if r not in reasons:
                reasons.append(r)

    status, reasons = _apply_device_status(device, status, reasons)

    shared = False
    shared_with: list[dict] = []
    if binding is not None:
        for key in _binding_share_keys(binding):
            if key in shared_keys:
                shared = True
                for peer in shared_peers.get(key, []):
                    if peer["device_id"] != device.device_id and peer not in shared_with:
                        shared_with.append(peer)
    if shared:
        if REASON_SHARED_ENDPOINT not in reasons:
            reasons.append(REASON_SHARED_ENDPOINT)
        if status == STATUS_AVAILABLE:
            status = STATUS_DEGRADED

    action_dicts = _action_dicts_for(kind, relevant)
    if (
        kind == KIND_POWER
        and status in (STATUS_AVAILABLE, STATUS_DEGRADED, STATUS_UNAVAILABLE)
        and relevant
    ):
        keys = {a["action_key"] for a in action_dicts}
        # 有 on/off 即可组合 cycle
        if "power_on" in keys and "power_off" in keys and "power_cycle" not in keys:
            action_dicts.append({
                "action_key": "power_cycle",
                "kind": "power",
                "enabled": True,
                "composed": True,
                "inter_step_ms": 0,
                "steps": [],
                "steps_sha256": _steps_sha([]),
                "constraints": {
                    "requires_lease": True,
                    "requires_admin": False,
                    "confirm": False,
                },
            })

    node = nodes.get(binding.node_id) if binding else None
    return {
        "id": cap_id,
        "kind": kind,
        "provider": "serial",
        "label": label,
        "status": status,
        "status_reasons": reasons,
        "transport": _transport_from(port, binding),
        "binding": _binding_view(binding, node, shared_keys, shared_with=shared_with),
        "actions": action_dicts,
        "health": _health_from(port, binding),
        "constraints": _default_constraints(read_only=False, shared=shared),
        "source": "declared",
        "detail_tool": "res_serial_caps",
        "detail_args": {"device_id": device.device_id},
        "observed_at": (
            binding.last_checked_at.isoformat()
            if binding and binding.last_checked_at else None
        ),
    }


def _action_dicts_for(kind: str, relevant: list[SerialAction]) -> list[dict]:
    out: list[dict] = []
    for a in relevant:
        composed = bool(a.action_key == "power_cycle" and not (a.steps or []))
        constraints = {
            "requires_lease": True,
            "requires_admin": a.action_key == "mode_edl",
            "confirm": a.action_key == "mode_edl",
        }
        out.append({
            "action_key": a.action_key,
            "display_name": a.display_name,
            "kind": a.kind or kind,
            "enabled": bool(a.enabled),
            "composed": composed,
            "inter_step_ms": int(a.inter_step_ms or 0),
            "steps": list(a.steps or []),
            "steps_sha256": _steps_sha(a.steps or []),
            "constraints": constraints,
        })
    return out


# ---- 状态 / 对齐辅助 -------------------------------------------------------

def _binding_status(binding: WireLogPortBinding) -> tuple[str, list[str]]:
    """将 WireLogPortBinding.health_state / reach_ok 映射为 capability status。

    未知 health_state 不得默认 available，避免 AI 误判可执行。
    """
    ep = (binding.endpoint or "").strip()
    if not ep:
        return STATUS_UNAVAILABLE, [REASON_NO_ENDPOINT]
    if binding.reach_ok is False:
        return STATUS_UNAVAILABLE, [REASON_ENDPOINT_UNREACHABLE]

    hs = (binding.health_state or "").strip().lower()
    if not hs or hs == "unknown":
        return STATUS_UNKNOWN, [REASON_NOT_PROBED]
    if hs == "open_failed":
        return STATUS_UNAVAILABLE, [REASON_ENDPOINT_OPEN_FAILED]
    if hs in _OFFLINE_HEALTH:
        return STATUS_UNAVAILABLE, [REASON_ENDPOINT_OFFLINE]
    if hs in _SUSPECT_HEALTH:
        return STATUS_DEGRADED, [REASON_HEALTH_SUSPECT]
    if hs in _OK_HEALTH:
        return STATUS_AVAILABLE, []
    # 未识别的 health_state：保守 degraded，勿当 available
    return STATUS_DEGRADED, [REASON_NOT_PROBED]


def _apply_device_status(
    device: Device, status: str, reasons: list[str]
) -> tuple[str, list[str]]:
    if device.status == DeviceStatus.RETIRED:
        if REASON_DEVICE_RETIRED not in reasons:
            reasons = reasons + [REASON_DEVICE_RETIRED]
        return STATUS_UNAVAILABLE, reasons
    if device.status == DeviceStatus.OFFLINE:
        if REASON_DEVICE_OFFLINE not in reasons:
            reasons = reasons + [REASON_DEVICE_OFFLINE]
        if status == STATUS_AVAILABLE:
            status = STATUS_DEGRADED
        elif status == STATUS_UNKNOWN:
            status = STATUS_UNAVAILABLE
    return status, reasons


def _merge_status(a: str, b: str) -> str:
    order = {
        STATUS_AVAILABLE: 0,
        STATUS_DEGRADED: 1,
        STATUS_UNKNOWN: 2,
        STATUS_UNAVAILABLE: 3,
    }
    return a if order.get(a, 9) >= order.get(b, 9) else b


def _worse(a: str, b: str) -> str:
    return _merge_status(a, b)


def _match_binding_to_declared(
    port: dict | DeviceSerialPort, bindings: list[WireLogPortBinding]
) -> WireLogPortBinding | None:
    if isinstance(port, DeviceSerialPort):
        endpoint = port.endpoint
        port_name = port.port_name
    else:
        endpoint = port.get("endpoint")
        port_name = port.get("port_name")
    ep = _norm_endpoint(endpoint)
    if ep:
        for b in bindings:
            if _norm_endpoint(b.endpoint) == ep:
                return b
            if b.com_key and _norm_endpoint(f"tcp://{b.com_key}") == ep:
                return b
    # com_key 次之：declared 无 com_key，用 port_name ≈ com_name
    if port_name:
        pn = port_name.strip().lower()
        for b in bindings:
            if (b.com_name or "").strip().lower() == pn:
                return b
            if (b.com_key or "").strip().lower() == pn:
                return b
    return None


def _norm_endpoint(endpoint: str | None) -> str | None:
    if not endpoint:
        return None
    ep = endpoint.strip().lower()
    if ep.startswith("tcp://"):
        ep = ep[6:]
    return ep or None


def _transport_from(
    port: DeviceSerialPort | None, binding: WireLogPortBinding | None
) -> dict | None:
    if binding is not None:
        return {
            "kind": "tcp",
            "endpoint": binding.endpoint,
            "port_name": binding.com_name,
            "channel": "serial",
            "baud": binding.baud,
        }
    if port is not None:
        return {
            "kind": port.transport or "tcp",
            "endpoint": port.endpoint,
            "port_name": port.port_name,
            "channel": port.channel or "serial",
            "baud": port.baudrate,
        }
    return None


def _binding_view(
    binding: WireLogPortBinding | None,
    node: WireLogNode | None,
    shared_keys: set[str],
    shared_with: list[dict] | None = None,
) -> dict | None:
    if binding is None:
        return None
    basis = None
    if binding.allocated_by and str(binding.allocated_by).startswith("auto:"):
        basis = str(binding.allocated_by).split(":", 1)[-1]
    view = {
        "node_id": binding.node_id,
        "node_ip": node.ip if node else None,
        "slot": binding.slot,
        "com": binding.com_name,
        "com_key": binding.com_key,
        "endpoint": binding.endpoint,
        "bound_device_id": binding.bound_device_id,
        "match_basis": basis,
        "source": "auto-bound" if basis else "observed",
    }
    if shared_with:
        view["shared_with"] = [
            {"device_id": p["device_id"], "slot": p["slot"], "label": p.get("label")}
            for p in shared_with
        ]
    return view


def _health_from(
    port: DeviceSerialPort | None, binding: WireLogPortBinding | None
) -> dict:
    b_state = (binding.health_state if binding else None) or "unknown"
    p_state = (port.health_state if port else None) or "unknown"
    # 取更差
    if _HEALTH_RANK.get(b_state, 1) >= _HEALTH_RANK.get(p_state, 1):
        state = b_state
    else:
        state = p_state
    reach_ok = binding.reach_ok if binding is not None else None
    error = None
    checked_at = None
    if binding is not None:
        error = binding.reach_error
        checked_at = (
            binding.last_checked_at.isoformat() if binding.last_checked_at
            else (binding.reach_checked_at.isoformat() if binding.reach_checked_at else None)
        )
    if port is not None and port.health_error and not error:
        error = port.health_error
    if port is not None and port.health_checked_at and not checked_at:
        checked_at = port.health_checked_at.isoformat()
    return {
        "state": state,
        "reach_ok": reach_ok,
        "error": error,
        "checked_at": checked_at,
    }


def _default_constraints(*, read_only: bool, shared: bool) -> dict:
    return {
        "requires_lease": True,
        "requires_admin": False,
        "confirm": False,
        "exclusive": bool(shared),
        "read_only": bool(read_only),
        "shared_endpoint": bool(shared),
    }


def _observed_channel_dict(
    b: WireLogPortBinding, node: WireLogNode | None
) -> dict:
    endpoint = b.endpoint
    if not endpoint and b.com_key:
        endpoint = f"tcp://{b.com_key}"
    elif not endpoint and node and node.ip and b.tcp_port:
        endpoint = f"tcp://{node.ip}:{b.tcp_port}"
    return {
        "channel": "serial",
        "com": b.com_name,
        "com_key": b.com_key,
        "endpoint": endpoint,
        "tcp_port": b.tcp_port,
        "baud": b.baud,
        "health_state": b.health_state,
        "reach_ok": b.reach_ok,
        "reach_error": b.reach_error,
        "fp_verdict": None,
        "identity": {
            "sn": None,
            "mac": b.shell_mac,
            "ip": b.shell_ip,
        },
        "state": "bound",
        "lease_id": None,
        "allocation": "bound",
        "allocated_expire": (
            b.allocated_expire.isoformat() if b.allocated_expire else None
        ),
        "source": {
            "node_id": b.node_id,
            "node_ip": node.ip if node else None,
            "display_name": (node.display_name if node else None) or b.node_id,
            "slot": b.slot,
            "console_url": (
                f"http://{node.ip}:8787/" if node and node.ip else None
            ),
        },
    }


# ---- summary / revision ----------------------------------------------------

def _summary_from_items(items: list[dict]) -> dict:
    def agg(kind: str, *, serial_alias: bool = False) -> str:
        if serial_alias:
            subset = [i for i in items if i["kind"] == KIND_SERIAL_IO]
        else:
            subset = [i for i in items if i["kind"] == kind]
        if not subset:
            return STATUS_UNAVAILABLE
        statuses = [i["status"] for i in subset]
        if STATUS_AVAILABLE in statuses:
            return STATUS_AVAILABLE
        if STATUS_DEGRADED in statuses:
            return STATUS_DEGRADED
        if STATUS_UNAVAILABLE in statuses:
            return STATUS_UNAVAILABLE
        return STATUS_UNKNOWN

    counts = {
        STATUS_AVAILABLE: 0,
        STATUS_DEGRADED: 0,
        STATUS_UNAVAILABLE: 0,
        STATUS_UNKNOWN: 0,
    }
    for i in items:
        st = i.get("status") or STATUS_UNKNOWN
        counts[st] = counts.get(st, 0) + 1
    return {
        "adb": agg(KIND_ADB),
        "serial": agg(KIND_SERIAL_IO, serial_alias=True),
        "power": agg(KIND_POWER),
        "mode": agg(KIND_MODE),
        "counts": counts,
    }


def _steps_sha(steps: Any) -> str:
    raw = json.dumps(steps or [], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _revision(items: list[dict]) -> str:
    rows = []
    for item in sorted(items, key=lambda x: x["id"]):
        actions_part = []
        for a in item.get("actions") or []:
            sha = a.get("steps_sha256") or _steps_sha(a.get("steps"))
            enabled = bool(a.get("enabled", True))
            inter = int(a.get("inter_step_ms") or 0)
            actions_part.append(f"{a.get('action_key')}:{enabled}:{sha}:{inter}")
        binding = item.get("binding") or {}
        health = item.get("health") or {}
        constraints = item.get("constraints") or {}
        bool_constraints = {
            k: bool(v) for k, v in constraints.items() if isinstance(v, bool)
        }
        rows.append([
            item.get("id"),
            item.get("kind"),
            item.get("status"),
            sorted(item.get("status_reasons") or []),
            actions_part,
            binding.get("endpoint"),
            binding.get("bound_device_id"),
            health.get("state"),
            health.get("reach_ok"),
            bool_constraints,
        ])
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return "cap1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def _public_item(item: dict) -> dict:
    """对外拷贝：去掉 steps 明文，保留 steps_sha256。"""
    out = dict(item)
    acts = []
    for a in item.get("actions") or []:
        aa = {k: v for k, v in a.items() if k != "steps"}
        acts.append(aa)
    out["actions"] = acts
    return out
