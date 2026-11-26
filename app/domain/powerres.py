"""PowerRes 电源资源域：独立于 WireLogPortBinding / COMRes。

登记「节点 + 电源设备 + 通道」的 TCP 控制端点，并绑定成被测设备上的
DeviceSerialPort(role=power)，供 capabilities 产出 power item。
"""
from __future__ import annotations

import time

from sqlalchemy.orm import Session

from app import serial_tcp
from app.domain import audit
from app.domain.catalog import DomainError
from app.domain.serial import _clamp_hold_ms
from app.models import (
    Device,
    DeviceSerialPort,
    PowerResBinding,
    PowerResInventory,
    SerialAction,
    WireLogNode,
)
from app.serial_tcp import SerialBusy, SerialConfigError, parse_endpoint
from app.utils import utcnow

SLOT_MIN = 1
SLOT_MAX = 16

LOCATION_MAX_LEN = 64
DRIVER_MAX_LEN = 32

# PT7a：通道级动作（admin 直控，不经 DeviceSerialPort）
CHANNEL_ACTIONS = ("power_on", "power_off", "power_cycle", "power_query")
_CHANNEL_OP = {
    "power_on": 0x01,
    "power_off": 0x00,
    "power_query": 0x05,
}
_CHANNEL_STATE_BY_ACTION = {
    "power_on": "on",
    "power_off": "off",
    "power_cycle": "on",
}
_CHANNEL_HOLD_TTL_SEC = 120.0
_CHANNEL_HOLD_WAIT_MS = 3000
_CHANNEL_HARD_TIMEOUT_S = 8.0


def _clean_location(value) -> str | None:
    """PT5a：location 规范化；超限报 POWERRES_BAD_PAYLOAD（不静默截断）。"""
    s = str(value).strip() if value is not None and str(value).strip() else None
    if s and len(s) > LOCATION_MAX_LEN:
        raise DomainError(
            "POWERRES_BAD_PAYLOAD",
            f"location 超长（≤{LOCATION_MAX_LEN}）",
        )
    return s


def _clean_driver(value) -> str | None:
    s = str(value).strip() if value is not None and str(value).strip() else None
    if s and len(s) > DRIVER_MAX_LEN:
        raise DomainError(
            "POWERRES_BAD_PAYLOAD",
            f"driver 超长（≤{DRIVER_MAX_LEN}）",
        )
    return s


def _parse_restart_hold_ms(value) -> int | None:
    """通道级 restart_hold_ms：仅采纳 int 且 >=0，否则 NULL（非法/缺失）。"""
    if value is None or value is False:
        return None
    if isinstance(value, bool):
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return v


def _inventory_meta(session: Session, node_id: str, power_dev: str) -> dict:
    """从 inventory 同 (node_id, power_dev) 行取设备级 location/driver（缺省 None）。"""
    row = (
        session.query(PowerResInventory)
        .filter(
            PowerResInventory.node_id == node_id,
            PowerResInventory.power_dev == power_dev,
        )
        .first()
    )
    return {
        "location": row.location if row else None,
        "driver": row.driver if row else None,
    }


def list_bindings(session: Session, node_id: str) -> list[dict]:
    _require_node(session, node_id)
    rows = (
        session.query(PowerResBinding)
        .filter(PowerResBinding.node_id == node_id)
        .order_by(PowerResBinding.power_dev, PowerResBinding.channel)
        .all()
    )
    return [_binding_dict(b) for b in rows]


def list_inventory(session: Session, node_id: str) -> list[dict]:
    _require_node(session, node_id)
    inv_rows = (
        session.query(PowerResInventory)
        .filter(PowerResInventory.node_id == node_id)
        .order_by(PowerResInventory.power_dev, PowerResInventory.channel)
        .all()
    )
    bindings = {
        (b.power_dev, b.channel): b
        for b in session.query(PowerResBinding)
        .filter(PowerResBinding.node_id == node_id)
        .all()
    }
    out: list[dict] = []
    for row in inv_rows:
        bound = bindings.get((row.power_dev, row.channel))
        out.append({
            "node_id": row.node_id,
            "power_dev": row.power_dev,
            "name": row.name,
            "com": row.com,
            "channel": row.channel,
            "endpoint": row.endpoint,
            "state": row.state,
            "location": row.location,
            "driver": row.driver,
            "restart_hold_ms": row.restart_hold_ms,
            "bound": bound is not None,
            "bound_slot": bound.slot if bound else None,
            "bound_device_id": bound.device_id if bound else None,
            "enabled": bool(bound.enabled) if bound is not None else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        })
    return out


def overview(session: Session) -> dict:
    """只返回有电源 inventory 或已有绑定的节点（供前端下拉）。"""
    inv_node_ids = {
        r[0]
        for r in session.query(PowerResInventory.node_id).distinct().all()
    }
    bind_node_ids = {
        r[0]
        for r in session.query(PowerResBinding.node_id).distinct().all()
    }
    node_ids = sorted(inv_node_ids | bind_node_ids)
    nodes: list[dict] = []
    for nid in node_ids:
        node = session.get(WireLogNode, nid)
        if node is None:
            continue
        inv_rows = (
            session.query(PowerResInventory)
            .filter(PowerResInventory.node_id == nid)
            .all()
        )
        bound_cnt = (
            session.query(PowerResBinding)
            .filter(PowerResBinding.node_id == nid)
            .count()
        )
        devices = {r.power_dev for r in inv_rows if r.power_dev}
        nodes.append({
            "node_id": nid,
            "display_name": node.display_name,
            "devices": len(devices),
            "channels": len(inv_rows),
            "bound": int(bound_cnt),
        })
    return {"ok": True, "nodes": nodes}


def upsert_inventory_item(
    session: Session,
    node_id: str,
    power_dev: str,
    channel: int,
    *,
    name: str | None = None,
    com: str | None = None,
    endpoint: str | None = None,
    state: str | None = None,
    location: str | None = None,
    driver: str | None = None,
) -> list[dict]:
    """手工登记单条电源资源：幂等 upsert（唯一键 node_id+power_dev+channel）。"""
    _require_node(session, node_id)
    power_dev = str(power_dev or "").strip()
    if not power_dev:
        raise DomainError("POWERRES_BAD_PAYLOAD", "power_dev 不能为空")
    try:
        channel = int(channel)
    except (TypeError, ValueError) as e:
        raise DomainError("POWERRES_BAD_PAYLOAD", f"非法 channel: {channel!r}") from e

    name_s = (str(name).strip() if name is not None and str(name).strip() else None)
    com_s = (str(com).strip() if com is not None and str(com).strip() else None)
    ep_raw = (str(endpoint).strip() if endpoint is not None else "")
    ep_s = _validate_endpoint(ep_raw) if ep_raw else None
    state_s = (
        str(state).strip()
        if state is not None and str(state).strip()
        else None
    )
    location_s = _clean_location(location)
    driver_s = _clean_driver(driver)

    now = utcnow()
    row = (
        session.query(PowerResInventory)
        .filter_by(node_id=node_id, power_dev=power_dev, channel=channel)
        .one_or_none()
    )
    if row is None:
        row = PowerResInventory(
            node_id=node_id,
            power_dev=power_dev,
            channel=channel,
            updated_at=now,
        )
        session.add(row)
    row.name = name_s
    row.com = com_s
    row.endpoint = ep_s
    row.state = state_s
    row.location = location_s
    row.driver = driver_s
    row.updated_at = now
    session.commit()
    return list_inventory(session, node_id)


def delete_inventory_item(
    session: Session,
    node_id: str,
    power_dev: str,
    channel: int,
) -> dict:
    """删除手工/上报的 inventory 行（不解绑；绑定独立）。"""
    _require_node(session, node_id)
    power_dev = str(power_dev or "").strip()
    try:
        channel = int(channel)
    except (TypeError, ValueError) as e:
        raise DomainError("POWERRES_BAD_PAYLOAD", f"非法 channel: {channel!r}") from e

    row = (
        session.query(PowerResInventory)
        .filter_by(node_id=node_id, power_dev=power_dev, channel=channel)
        .one_or_none()
    )
    if row is None:
        raise DomainError(
            "NOT_FOUND",
            f"powerres inventory not found: {node_id}/{power_dev}/ch{channel}",
        )
    session.delete(row)
    session.commit()
    return {
        "ok": True,
        "deleted": {
            "node_id": node_id,
            "power_dev": power_dev,
            "channel": channel,
        },
        "inventory": list_inventory(session, node_id),
    }


def sync_inventory(session: Session, node_id: str, devices: list) -> dict:
    """节点侧覆盖写 inventory：幂等 upsert；消失的 (power_dev, channel) 清理。"""
    _require_node(session, node_id)
    if not isinstance(devices, list):
        raise DomainError("POWERRES_BAD_PAYLOAD", "devices 必须是数组")

    now = utcnow()
    seen: set[tuple[str, int]] = set()
    upserted = 0
    for raw in devices:
        if not isinstance(raw, dict):
            continue
        power_dev = str(raw.get("id") or raw.get("power_dev") or "").strip()
        if not power_dev:
            raise DomainError("POWERRES_BAD_PAYLOAD", "devices[].id 不能为空")
        name = (str(raw.get("name") or "").strip() or None)
        com = (str(raw.get("com") or "").strip() or None)
        # PT5a：设备级 location/driver（契约 ≤64/≤32，缺省 None）
        location_s = _clean_location(raw.get("location"))
        driver_s = _clean_driver(raw.get("driver"))
        channels = raw.get("channels") or []
        if not isinstance(channels, list):
            raise DomainError(
                "POWERRES_BAD_PAYLOAD",
                f"devices[{power_dev}].channels 必须是数组",
            )
        for ch_raw in channels:
            if not isinstance(ch_raw, dict):
                continue
            try:
                channel = int(ch_raw.get("ch") if "ch" in ch_raw else ch_raw.get("channel"))
            except (TypeError, ValueError) as e:
                raise DomainError(
                    "POWERRES_BAD_PAYLOAD",
                    f"非法 channel: {ch_raw!r}",
                ) from e
            endpoint = (str(ch_raw.get("endpoint") or "").strip() or None)
            state = ch_raw.get("state")
            state_s = (str(state).strip() if state is not None and str(state).strip() else None)
            hold_ms = _parse_restart_hold_ms(ch_raw.get("restart_hold_ms"))
            key = (power_dev, channel)
            seen.add(key)
            row = (
                session.query(PowerResInventory)
                .filter_by(node_id=node_id, power_dev=power_dev, channel=channel)
                .one_or_none()
            )
            if row is None:
                row = PowerResInventory(
                    node_id=node_id,
                    power_dev=power_dev,
                    channel=channel,
                    updated_at=now,
                )
                session.add(row)
            row.name = name
            row.com = com
            row.endpoint = endpoint
            row.state = state_s
            row.location = location_s
            row.driver = driver_s
            row.restart_hold_ms = hold_ms
            row.updated_at = now
            upserted += 1

    # 清理本节点未再上报的 inventory 行
    existing = (
        session.query(PowerResInventory)
        .filter(PowerResInventory.node_id == node_id)
        .all()
    )
    removed = 0
    for row in existing:
        if (row.power_dev, row.channel) not in seen:
            session.delete(row)
            removed += 1

    session.commit()
    return {
        "node_id": node_id,
        "upserted": upserted,
        "removed": removed,
        "inventory": list_inventory(session, node_id),
    }


def bind(
    session: Session,
    node_id: str,
    power_dev: str,
    channel: int,
    endpoint: str,
    *,
    device_id: str,
    label: str | None = None,
    com: str | None = None,
    slot: int | None = None,
) -> dict:
    """绑定电源通道到被测设备：写 PowerResBinding + upsert DeviceSerialPort(role=power)。"""
    _require_node(session, node_id)
    device_id = str(device_id or "").strip()
    if not device_id:
        raise DomainError("POWERRES_BAD_PAYLOAD", "device_id 不能为空")
    device = session.get(Device, device_id)
    if device is None:
        raise DomainError("NOT_FOUND", f"device {device_id} not found")

    power_dev = str(power_dev or "").strip()
    if not power_dev:
        raise DomainError("POWERRES_BAD_PAYLOAD", "power_dev 不能为空")
    try:
        channel = int(channel)
    except (TypeError, ValueError) as e:
        raise DomainError("POWERRES_BAD_PAYLOAD", f"非法 channel: {channel!r}") from e

    ep = _validate_endpoint(endpoint)
    label_s = (str(label).strip() if label else "") or f"{power_dev} CH{channel}"
    com_s = (str(com).strip() if com else None) or None

    existing = (
        session.query(PowerResBinding)
        .filter_by(node_id=node_id, power_dev=power_dev, channel=channel)
        .one_or_none()
    )
    if existing is not None:
        # 复用原 slot；若换了 device 则转到新 device（仍复用 slot 号语义在原 device 上）
        use_slot = existing.slot
        if existing.device_id != device_id:
            # 旧 device 端口禁用，新 device 重新分配/占用同语义
            _disable_power_port(session, existing.device_id, existing.slot)
            use_slot = _allocate_slot(session, device_id, prefer=slot, reuse=None)
        target_device_id = device_id
    else:
        use_slot = _allocate_slot(session, device_id, prefer=slot, reuse=None)
        target_device_id = device_id

    now = utcnow()
    port = _upsert_power_port(
        session,
        device_id=target_device_id,
        slot=use_slot,
        endpoint=ep,
        channel=channel,
        label=label_s,
        com=com_s,
    )
    _ensure_default_power_actions(session, port, channel)

    if existing is None:
        binding = PowerResBinding(
            node_id=node_id,
            device_id=target_device_id,
            slot=use_slot,
            power_dev=power_dev,
            channel=channel,
            endpoint=ep,
            com=com_s,
            label=label_s,
            enabled=True,
            created_at=now,
            updated_at=now,
        )
        session.add(binding)
    else:
        binding = existing
        binding.device_id = target_device_id
        binding.slot = use_slot
        binding.endpoint = ep
        binding.com = com_s
        binding.label = label_s
        binding.enabled = True
        binding.updated_at = now

    session.commit()
    return {
        "binding": _binding_dict(binding),
        "port": {
            "device_id": port.device_id,
            "slot": port.slot,
            "role": port.role,
            "endpoint": port.endpoint,
            "channel": port.channel,
            "label": port.label,
            "enabled": bool(port.enabled),
        },
    }


def unbind(session: Session, node_id: str, power_dev: str, channel: int) -> dict:
    power_dev = str(power_dev or "").strip()
    try:
        channel = int(channel)
    except (TypeError, ValueError) as e:
        raise DomainError("POWERRES_BAD_PAYLOAD", f"非法 channel: {channel!r}") from e

    binding = (
        session.query(PowerResBinding)
        .filter_by(node_id=node_id, power_dev=power_dev, channel=channel)
        .one_or_none()
    )
    if binding is None:
        raise DomainError(
            "NOT_FOUND",
            f"powerres binding not found: {node_id}/{power_dev}/ch{channel}",
        )

    port = _disable_power_port(session, binding.device_id, binding.slot)
    brief = _binding_dict(binding)
    session.delete(binding)
    session.commit()
    return {
        "binding": brief,
        "port": (
            {
                "device_id": port.device_id,
                "slot": port.slot,
                "role": port.role,
                "enabled": bool(port.enabled),
            }
            if port is not None
            else None
        ),
    }


def channel_action(
    session: Session,
    node_id: str,
    power_dev: str,
    channel: int,
    action: str,
    *,
    by: str,
    hold_ms: int | None = None,
) -> dict:
    """PT7a：管理员通道级动作——直接对通道 endpoint 发继电器帧，不经 DeviceSerialPort。

    占用键 ``powerres:{node_id}:{power_dev}:{channel}``，与设备串口 holder 隔离。
    已绑定且 ``enabled=False`` → ``POWER_DISABLED``；未绑定可直接执行。
    """
    _require_node(session, node_id)
    power_dev = str(power_dev or "").strip()
    if not power_dev:
        raise DomainError("POWERRES_BAD_PAYLOAD", "power_dev 不能为空")
    try:
        channel = int(channel)
    except (TypeError, ValueError) as e:
        raise DomainError("POWERRES_BAD_PAYLOAD", f"非法 channel: {channel!r}") from e

    action_key = str(action or "").strip()
    detail_base = {
        "node_id": node_id,
        "power_dev": power_dev,
        "channel": channel,
        "action": action_key or str(action),
    }
    t0 = time.monotonic()

    def _audit_fail(error_code: str, **extra) -> None:
        d = {
            **detail_base,
            "result": "failed",
            "error_code": error_code,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            **{k: v for k, v in extra.items() if v is not None},
        }
        audit.record(
            session,
            actor=by,
            action="powerres_channel_action",
            detail=d,
        )
        session.commit()

    def _reject(code: str, message: str, detail: dict | None = None) -> None:
        _audit_fail(code)
        raise DomainError(code, message, detail or {
            "node_id": node_id, "power_dev": power_dev, "channel": channel,
        })

    if action_key not in CHANNEL_ACTIONS:
        _reject(
            "POWERRES_BAD_ACTION",
            f"非法 action: {action!r}（允许 {', '.join(CHANNEL_ACTIONS)}）",
        )

    inv = (
        session.query(PowerResInventory)
        .filter_by(node_id=node_id, power_dev=power_dev, channel=channel)
        .one_or_none()
    )
    if inv is None:
        _reject(
            "POWERRES_UNKNOWN_CHANNEL",
            f"未知通道: {node_id}/{power_dev}/ch{channel}",
        )
    endpoint = (inv.endpoint or "").strip()
    if not endpoint:
        _reject(
            "POWERRES_NO_ENDPOINT",
            f"通道端点缺失: {node_id}/{power_dev}/ch{channel}",
        )

    binding = (
        session.query(PowerResBinding)
        .filter_by(node_id=node_id, power_dev=power_dev, channel=channel)
        .one_or_none()
    )
    if binding is not None and not bool(binding.enabled):
        _reject(
            "POWER_DISABLED",
            f"通道 {power_dev} CH{channel} 已停用，拒绝电源动作",
        )

    # PT8-tres：已注册通道强制用 inventory.restart_hold_ms；手工通道仍用请求 hold
    if inv.restart_hold_ms is not None:
        hold = _clamp_hold_ms(int(inv.restart_hold_ms))
    else:
        hold = _clamp_hold_ms(hold_ms)
    hold_key = f"powerres:{node_id}:{power_dev}:{channel}"
    acquired = False

    try:
        try:
            serial_tcp.acquire_wait(
                hold_key, by, _CHANNEL_HOLD_TTL_SEC, _CHANNEL_HOLD_WAIT_MS,
            )
        except SerialBusy as e:
            holder = (e.detail or {}).get("holder")
            _audit_fail(
                "POWERRES_BUSY",
                hold_ms=hold if action_key == "power_cycle" else None,
            )
            raise DomainError(
                "POWERRES_BUSY",
                f"通道 {power_dev} CH{channel} 正被 {holder} 占用",
                {"holder": holder, **(e.detail or {})},
            ) from e
        acquired = True

        target = ("tcp", endpoint)
        state_out: str | None = None
        exec_detail: dict = {}

        if action_key == "power_cycle":
            # off → hold → on；一次 acquire 覆盖全程（两次 execute_steps + sleep）
            r_off = serial_tcp.execute_steps(
                target,
                [{"data": _relay_frame_hex(channel, 0x00), "encoding": "hex"}],
                default_encoding="hex",
                baudrate=115200,
                hard_timeout_s=_CHANNEL_HARD_TIMEOUT_S,
            )
            if not r_off.ok:
                code = getattr(r_off, "error_code", None) or "SERIAL_WRITE"
                _audit_fail(
                    code,
                    hold_ms=hold,
                    phase="power_off",
                    error=getattr(r_off, "error", None),
                )
                return {
                    **detail_base,
                    "result": "failed",
                    "error_code": code,
                    "error": getattr(r_off, "error", None),
                    "phase": "power_off",
                    "hold_ms": hold,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "state": inv.state,
                }
            time.sleep(hold / 1000.0)
            r_on = serial_tcp.execute_steps(
                target,
                [{"data": _relay_frame_hex(channel, 0x01), "encoding": "hex"}],
                default_encoding="hex",
                baudrate=115200,
                hard_timeout_s=_CHANNEL_HARD_TIMEOUT_S,
            )
            if not r_on.ok:
                code = getattr(r_on, "error_code", None) or "SERIAL_WRITE"
                _audit_fail(
                    code,
                    hold_ms=hold,
                    phase="power_on",
                    error=getattr(r_on, "error", None),
                )
                return {
                    **detail_base,
                    "result": "failed",
                    "error_code": code,
                    "error": getattr(r_on, "error", None),
                    "phase": "power_on",
                    "hold_ms": hold,
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "state": inv.state,
                }
            state_out = "on"
            exec_detail["hold_ms"] = hold
        else:
            op = _CHANNEL_OP[action_key]
            steps = [{"data": _relay_frame_hex(channel, op), "encoding": "hex"}]
            if action_key == "power_query":
                # 等应答：任意非空回包；解析 A0|ch|state|sum
                steps[0].update({
                    "expect_mode": "regex",
                    "expect": r".+",
                    "timeout_ms": 800,
                })
            r = serial_tcp.execute_steps(
                target,
                steps,
                default_encoding="hex",
                baudrate=115200,
                hard_timeout_s=_CHANNEL_HARD_TIMEOUT_S,
            )
            if not r.ok:
                code = getattr(r, "error_code", None) or "SERIAL_WRITE"
                _audit_fail(code, error=getattr(r, "error", None))
                return {
                    **detail_base,
                    "result": "failed",
                    "error_code": code,
                    "error": getattr(r, "error", None),
                    "duration_ms": int((time.monotonic() - t0) * 1000),
                    "state": inv.state,
                }
            if action_key == "power_query":
                state_out = _parse_query_state(
                    getattr(r, "response_tail", "") or "",
                    channel,
                ) or inv.state
                exec_detail["response_tail"] = getattr(r, "response_tail", "") or ""
            else:
                state_out = _CHANNEL_STATE_BY_ACTION[action_key]

        # 成功：更新 inventory.state / updated_at
        if state_out is not None:
            inv.state = state_out
        inv.updated_at = utcnow()
        duration_ms = int((time.monotonic() - t0) * 1000)
        audit_detail = {
            **detail_base,
            "result": "ok",
            "duration_ms": duration_ms,
            "state": inv.state,
            **exec_detail,
        }
        if action_key == "power_cycle":
            audit_detail["hold_ms"] = hold
        audit.record(
            session,
            actor=by,
            action="powerres_channel_action",
            detail=audit_detail,
        )
        session.commit()
        out = {
            **detail_base,
            "result": "ok",
            "state": inv.state,
            "duration_ms": duration_ms,
            "endpoint": endpoint,
        }
        if action_key == "power_cycle":
            out["hold_ms"] = hold
        return out
    finally:
        if acquired:
            serial_tcp.release(hold_key, by)


def _parse_query_state(response: str, channel: int) -> str | None:
    """从 query 应答解析状态。优先 ASCII hex（如 A00101A2），其次 latin-1 字节流。"""
    if not response:
        return None

    def _from_bytes(raw: bytes) -> str | None:
        if not raw or len(raw) < 4:
            return None
        for i in range(0, len(raw) - 3):
            if raw[i] != 0xA0:
                continue
            if raw[i + 1] != int(channel):
                continue
            st = raw[i + 2]
            chk = (0xA0 + int(channel) + st) & 0xFF
            if raw[i + 3] != chk:
                continue
            if st == 0x01:
                return "on"
            if st == 0x00:
                return "off"
            return f"0x{st:02X}"
        return None

    hx = "".join(c for c in str(response).upper() if c in "0123456789ABCDEF")
    if len(hx) >= 8:
        try:
            got = _from_bytes(bytes.fromhex(hx[: len(hx) - (len(hx) % 2)]))
            if got:
                return got
        except ValueError:
            pass
    try:
        got = _from_bytes(str(response).encode("latin-1", errors="ignore"))
        if got:
            return got
    except Exception:
        pass
    return None


# ---- internals -------------------------------------------------------------

def _require_node(session: Session, node_id: str) -> WireLogNode:
    node = session.get(WireLogNode, node_id)
    if node is None:
        raise DomainError("NOT_FOUND", f"node {node_id} not found")
    return node


def _validate_endpoint(endpoint: str) -> str:
    ep = (str(endpoint or "").strip())
    if not ep:
        raise DomainError("POWERRES_BAD_ENDPOINT", "endpoint 为空")
    try:
        parse_endpoint(ep)
    except SerialConfigError as e:
        raise DomainError("POWERRES_BAD_ENDPOINT", e.message) from e
    return ep


def _allocate_slot(
    session: Session,
    device_id: str,
    *,
    prefer: int | None,
    reuse: int | None,
) -> int:
    if reuse is not None:
        return int(reuse)

    ports = {
        p.slot: p
        for p in session.query(DeviceSerialPort)
        .filter(DeviceSerialPort.device_id == device_id)
        .all()
    }

    candidates: list[int] = []
    if prefer is not None:
        try:
            pref = int(prefer)
        except (TypeError, ValueError) as e:
            raise DomainError("POWERRES_BAD_PAYLOAD", f"非法 slot: {prefer!r}") from e
        if pref < SLOT_MIN or pref > SLOT_MAX:
            raise DomainError(
                "POWERRES_BAD_PAYLOAD",
                f"slot 越界: {pref}（允许 {SLOT_MIN}..{SLOT_MAX}）",
            )
        candidates.append(pref)
    candidates.extend(s for s in range(SLOT_MIN, SLOT_MAX + 1) if s not in candidates)

    for s in candidates:
        existing = ports.get(s)
        if existing is None:
            return s
        # 已属于别的 role → 跳过，不覆盖
        if (existing.role or "").strip().lower() != "power":
            continue
        # 同 role=power：仅回收已禁用的历史端口
        if not existing.enabled:
            return s
        # 启用中的 power 口占用中 → 跳过
        continue

    raise DomainError("POWERRES_NO_SLOT", "no free slot")


def _upsert_power_port(
    session: Session,
    *,
    device_id: str,
    slot: int,
    endpoint: str,
    channel: int,
    label: str,
    com: str | None,
) -> DeviceSerialPort:
    port = (
        session.query(DeviceSerialPort)
        .filter(
            DeviceSerialPort.device_id == device_id,
            DeviceSerialPort.slot == slot,
        )
        .first()
    )
    if port is None:
        port = DeviceSerialPort(
            device_id=device_id,
            slot=slot,
            created_at=utcnow(),
        )
        session.add(port)
    elif (port.role or "").strip().lower() not in ("power", ""):
        # 防御：分配逻辑应已跳过，这里再挡一次
        raise DomainError(
            "POWERRES_SLOT_CONFLICT",
            f"slot {slot} 已属于 role={port.role}，不能覆盖",
        )

    port.role = "power"
    port.label = label
    port.transport = "tcp"
    port.endpoint = endpoint
    port.port_name = com
    port.channel = f"ch{channel}"
    port.baudrate = 115200
    port.encoding_default = "hex"
    port.enabled = True
    session.flush()
    return port


def _disable_power_port(
    session: Session, device_id: str, slot: int
) -> DeviceSerialPort | None:
    port = (
        session.query(DeviceSerialPort)
        .filter(
            DeviceSerialPort.device_id == device_id,
            DeviceSerialPort.slot == slot,
        )
        .first()
    )
    if port is not None:
        port.enabled = False
        session.flush()
    return port


def _ensure_default_power_actions(
    session: Session, port: DeviceSerialPort, channel: int
) -> None:
    """若尚无 power_on/off，按继电器 A0|ch|op|sum 帧补默认动作。

    steps 写规范：``{"data": "<hex>", "encoding": "hex"}``。
    ``power_cycle`` 不落库 steps——执行层按 power_off → hold → power_on 组合。
    """
    existing = {
        a.action_key
        for a in session.query(SerialAction).filter(SerialAction.port_id == port.id).all()
    }
    defaults = (
        ("power_on", 0x01),
        ("power_off", 0x00),
    )
    for key, op in defaults:
        if key in existing:
            continue
        hx = _relay_frame_hex(channel, op)
        session.add(SerialAction(
            port_id=port.id,
            action_key=key,
            display_name=key,
            kind="power",
            steps=[{"data": hx, "encoding": "hex"}],
            inter_step_ms=0,
            enabled=True,
            sort=0,
        ))
    # composed power_cycle：空 steps，执行时 off→hold→on；有则跳过
    if "power_cycle" not in existing:
        session.add(SerialAction(
            port_id=port.id,
            action_key="power_cycle",
            display_name="power_cycle",
            kind="power",
            steps=[],
            inter_step_ms=0,
            enabled=True,
            sort=0,
        ))
    session.flush()


def _relay_frame_hex(channel: int, op: int) -> str:
    start = 0xA0
    chk = (start + int(channel) + int(op)) & 0xFF
    return f"{start:02X}{int(channel):02X}{int(op):02X}{chk:02X}"


def _binding_dict(b: PowerResBinding) -> dict:
    d = {
        "id": b.id,
        "node_id": b.node_id,
        "device_id": b.device_id,
        "slot": b.slot,
        "power_dev": b.power_dev,
        "channel": b.channel,
        "endpoint": b.endpoint,
        "com": b.com,
        "label": b.label,
        "enabled": bool(b.enabled),
        "created_at": b.created_at.isoformat() if b.created_at else None,
        "updated_at": b.updated_at.isoformat() if b.updated_at else None,
    }
    bind_session = Session.object_session(b)
    if bind_session is not None:
        d.update(_inventory_meta(bind_session, b.node_id, b.power_dev))
    return d
