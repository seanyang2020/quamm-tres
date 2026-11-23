"""设备资源包（device bundle）：把同一台被测设备的 ADB 与串口成套聚合。

口径：
- ADB 通道来自 Device（ip + adb_port），串口通道来自 COMRes 绑定（bound_device_id）；
- 串口随设备一起呈现，但归属仍以绑定为准（不复制分配语义）；
- source 只做溯源：节点 display_name / node_id / 接入方式，前端可点开控制台。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.domain import comres
from app.models import Device, Lease, LeaseState, ProductType, WireLogNode

_ACTIVE = tuple(sorted(LeaseState.ACTIVE))


def _serial_channel(item: dict, lease_id: str | None = None) -> dict:
    node = item.get("node_id")
    return {
        "channel": "serial",
        "com": item.get("com"),
        "com_key": item.get("com_key"),
        "endpoint": item.get("tcp_port") and item.get("node_ip")
        and f"tcp://{item['node_ip']}:{item['tcp_port']}",
        "tcp_port": item.get("tcp_port"),
        "baud": item.get("baud"),
        "health_state": item.get("health_state"),
        "reach_ok": item.get("reach_ok"),
        "reach_error": item.get("reach_error"),
        "fp_verdict": item.get("fp_verdict"),
        "identity": {
            "sn": item.get("sn"),
            "mac": item.get("mac"),
            "ip": item.get("ip"),
        },
        "state": item.get("state"),
        "lease_id": lease_id,
        "allocation": "with_device" if lease_id else "bound",
        "allocated_expire": item.get("allocated_expire"),
        "source": {
            "node_id": node,
            "node_ip": item.get("node_ip"),
            "display_name": item.get("node_display_name") or node,
            "slot": item.get("slot"),
            "console_url": (
                f"http://{item['node_ip']}:8787/" if item.get("node_ip") else None
            ),
        },
    }


def _adb_channel(device: Device) -> dict:
    return {
        "channel": "adb",
        "address": device.adb_address,
        "ip": device.ip,
        "port": device.adb_port,
        "status": device.status,
        "health_level": device.health_level,
        "mac": device.mac,
        "model_class": device.model_class,
    }


def _active_lease(session: Session, device_id: str) -> dict | None:
    lease = (
        session.query(Lease)
        .filter(Lease.device_id == device_id, Lease.state.in_(_ACTIVE))
        .order_by(Lease.granted_at.desc())
        .first()
    )
    if lease is None:
        return None
    return {
        "lease_id": lease.lease_id,
        "state": lease.state,
        "adb_address": lease.adb_address,
        "agent_label": lease.credential.agent_label if lease.credential else None,
        "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
    }


def build_bundles(session: Session) -> dict:
    """全量设备资源包视图（Web 与 MCP 共用）。"""
    resources = comres.list_resources(session)
    node_names = {
        n.node_id: (n.display_name or n.node_id)
        for n in session.query(WireLogNode).all()
    }
    by_device: dict[str, list[dict]] = {}
    for bucket in ("allocated", "bound", "inactive"):
        for item in resources.get(bucket, []):
            dev = item.get("bound_device_id") or item.get("device_id")
            if dev:
                item = {**item, "node_display_name": node_names.get(item.get("node_id"))}
                by_device.setdefault(dev, []).append(item)

    devices = (
        session.query(Device)
        .filter(Device.retired_at.is_(None))
        .order_by(Device.device_id)
        .all()
    )
    pt_names = {
        p.type_code: p.display_name for p in session.query(ProductType).all()
    }
    bundles = []
    for d in devices:
        lease = _active_lease(session, d.device_id)
        serials = [
            _serial_channel(x, lease["lease_id"] if lease else None)
            for x in by_device.get(d.device_id, [])
        ]
        bundles.append({
            "device_id": d.device_id,
            "identity": {
                "display_name": d.display_name,
                "mac": d.mac,
                "sn": d.sn,
                "model_class": d.model_class,
                "model_name": pt_names.get(d.model_class),
                "ip": d.ip,
            },
            "status": {
                "status": d.status,
                "health_level": d.health_level,
                "source": d.source,
                "last_seen_at": (
                    d.last_seen_at.isoformat() if d.last_seen_at else None
                ),
            },
            "channels": {
                "adb": _adb_channel(d),
                "serial": serials,
            },
            "lease": lease,
            "complete": bool(serials),
        })

    # 未绑定任何设备的串口：单独一栏，避免"看着像没纳管"
    unattached = [
        _serial_channel(x)
        for bucket in ("bound", "allocated", "pool")
        for x in resources.get(bucket, [])
        if not (x.get("bound_device_id") or x.get("device_id"))
    ]
    return {
        "devices": bundles,
        "unattached_serial": unattached,
        "summary": {
            "devices": len(bundles),
            "with_serial": sum(1 for b in bundles if b["complete"]),
            "serial_bound": sum(
                len(b["channels"]["serial"]) for b in bundles
            ),
            "serial_unattached": len(unattached),
        },
    }


def device_bundle(session: Session, device_id: str) -> dict:
    device = session.get(Device, device_id)
    if device is None:
        from app.utils import DomainError

        raise DomainError("DEVICE_NOT_FOUND", f"设备不存在: {device_id}")
    bundles = build_bundles(session)
    for b in bundles["devices"]:
        if b["device_id"] == device_id:
            return b
    raise DomainError("DEVICE_NOT_FOUND", f"设备不存在: {device_id}")
