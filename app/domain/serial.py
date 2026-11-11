"""设备层串口能力（领域层）。

职责边界：
- 这里管**定义**（设备↔端口↔动作）、**执行**（调 app.serial_tcp）、**状态与审计**；
- 权限（谁能调）由 API/MCP 层按租约与 admin 判定，本层只收 `by` 与 `lease_id` 用于留痕；
- 不假设 tres-logs 的实现在哪，只要求它把每路串口映射成 tcp://host:port。
"""
from __future__ import annotations

import time
from typing import Any

from sqlalchemy.orm import Session

from app import serial_tcp
from app.domain import audit
from app.domain.catalog import DomainError
from app.models import (
    Device,
    DeviceSerialPort,
    SerialAction,
    SerialLogCursor,
    SerialState,
)
from app.utils import utcnow

VALID_ROLES = ("log", "power", "mode", "spare")
VALID_TRANSPORTS = ("tcp", "local")
POWER_KEYS = ("power_on", "power_off", "power_cycle")
MODE_KEYS = ("mode_normal", "mode_fastboot", "mode_edl")
POWER_CYCLE_HOLD_MS = 1200   # 断电保持；电源板电容释放一般 1~3s，必要时调
DEFAULT_TTL_SEC = 120.0
DEFAULT_HARD_TIMEOUT_S = 5.0

_POWER_STATE_BY_KEY = {"power_on": "on", "power_off": "off", "power_cycle": "on"}
_MODE_STATE_BY_KEY = {
    "mode_normal": "normal",
    "mode_fastboot": "fastboot",
    "mode_edl": "edl",
}


# ---------------------------------------------------------------- 基础

def _device(session: Session, device_id: str) -> Device:
    d = session.get(Device, device_id)
    if d is None:
        raise DomainError("NOT_FOUND", f"device {device_id} not found")
    return d


def _port_target(port: DeviceSerialPort) -> tuple[str, str]:
    """port -> (transport, 目标)。tcp 用 endpoint，local 用 port_name。"""
    if port.transport == "local":
        if not port.port_name:
            raise DomainError("SERIAL_CONFIG", f"串口 {port.slot} 缺 port_name")
        return ("local", port.port_name)
    if not port.endpoint:
        raise DomainError(
            "SERIAL_CONFIG",
            f"串口 {port.slot} 没有 endpoint（tres-logs 侧的 tcp://host:port）",
        )
    return ("tcp", port.endpoint)


def _hold_key(port: DeviceSerialPort) -> str:
    """占用键用物理通道而不是设备槽位：两台设备误配同一端点时也能防串台。"""
    if port.transport == "local":
        return f"local:{port.port_name or ('slot%s' % port.slot)}"
    return f"tcp:{(port.endpoint or '').strip().lower() or ('slot%s' % port.slot)}"


def _port_dict(port: DeviceSerialPort, actions: list[SerialAction] | None = None) -> dict:
    return {
        "slot": port.slot,
        "role": port.role,
        "label": port.label,
        "transport": port.transport,
        "endpoint": port.endpoint,
        "port_name": port.port_name,
        "channel": port.channel,
        "baudrate": port.baudrate,
        "databits": port.databits,
        "stopbits": port.stopbits,
        "parity": port.parity,
        "encoding_default": port.encoding_default,
        "enabled": bool(port.enabled),
        "actions": [
            {
                "action_key": a.action_key,
                "display_name": a.display_name,
                "kind": a.kind,
                "steps": a.steps or [],
                "inter_step_ms": a.inter_step_ms,
                "enabled": bool(a.enabled),
                "sort": a.sort,
            }
            for a in (actions or [])
            if a.enabled
        ],
    }


def list_ports(session: Session, device_id: str) -> list[dict]:
    _device(session, device_id)
    ports = (
        session.query(DeviceSerialPort)
        .filter(DeviceSerialPort.device_id == device_id)
        .order_by(DeviceSerialPort.slot)
        .all()
    )
    acts = {
        p.id: session.query(SerialAction)
        .filter(SerialAction.port_id == p.id)
        .order_by(SerialAction.sort, SerialAction.id)
        .all()
        for p in ports
    }
    return [_port_dict(p, acts.get(p.id)) for p in ports]


# ---------------------------------------------------------------- 配置（admin）

def upsert_ports(
    session: Session,
    device_id: str,
    ports: list[dict],
    *,
    actor: str,
    replace: bool = False,
) -> dict:
    """按 slot 合并（默认）或全量替换端口与动作。

    传 replace=True 时，未出现在 ports 里的 slot 会被删除（连同其动作）。
    """
    _device(session, device_id)
    if not isinstance(ports, list):
        raise DomainError("SERIAL_CONFIG", "ports 必须是数组")
    seen: set[int] = set()
    for raw in ports:
        if not isinstance(raw, dict):
            raise DomainError("SERIAL_CONFIG", "ports 里每一项都必须是对象")
        try:
            slot = int(raw.get("slot"))
        except (TypeError, ValueError):
            raise DomainError("SERIAL_CONFIG", "每个端口必须有整数 slot")
        if slot < 1 or slot > 16:
            raise DomainError("SERIAL_CONFIG", f"slot 越界: {slot}（1..16）")
        role = str(raw.get("role") or "spare")
        if role not in VALID_ROLES:
            raise DomainError("SERIAL_CONFIG", f"role 非法: {role}（{'/'.join(VALID_ROLES)}）")
        transport = str(raw.get("transport") or "tcp")
        if transport not in VALID_TRANSPORTS:
            raise DomainError("SERIAL_CONFIG", f"transport 非法: {transport}")
        endpoint = (str(raw.get("endpoint") or "").strip() or None)
        port_name = (str(raw.get("port_name") or "").strip() or None)
        if transport == "tcp":
            if not endpoint:
                raise DomainError("SERIAL_CONFIG", f"slot {slot} 是 tcp，必须给 endpoint")
            try:
                serial_tcp.parse_endpoint(endpoint)
            except serial_tcp.SerialConfigError as e:
                raise DomainError("SERIAL_CONFIG", e.message) from e
        elif not port_name:
            raise DomainError("SERIAL_CONFIG", f"slot {slot} 是 local，必须给 port_name")
        if slot in seen:
            raise DomainError("SERIAL_CONFIG", f"slot 重复: {slot}")
        seen.add(slot)

        port = (
            session.query(DeviceSerialPort)
            .filter(
                DeviceSerialPort.device_id == device_id,
                DeviceSerialPort.slot == slot,
            )
            .first()
        )
        if port is None:
            port = DeviceSerialPort(device_id=device_id, slot=slot, created_at=utcnow())
            session.add(port)
        port.role = role
        port.label = (str(raw.get("label") or "").strip() or None)
        port.transport = transport
        port.endpoint = endpoint
        port.port_name = port_name
        port.channel = (str(raw.get("channel") or "").strip() or None)
        port.baudrate = int(raw.get("baudrate") or 115200)
        port.databits = int(raw.get("databits") or 8)
        port.stopbits = int(raw.get("stopbits") or 1)
        port.parity = str(raw.get("parity") or "N")[:1].upper()
        enc = str(raw.get("encoding_default") or "hex").lower()
        port.encoding_default = enc if enc in ("hex", "text") else "hex"
        port.enabled = bool(raw.get("enabled", True))
        session.flush()

        acts = raw.get("actions")
        if acts is not None:
            if not isinstance(acts, list):
                raise DomainError("SERIAL_CONFIG", f"slot {slot} 的 actions 必须是数组")
            for a in acts:
                _upsert_action(session, port, a)
            keep = {str(a.get("action_key")) for a in acts if isinstance(a, dict)}
            for old in session.query(SerialAction).filter(SerialAction.port_id == port.id).all():
                if old.action_key not in keep:
                    session.delete(old)

    if replace:
        for old in (
            session.query(DeviceSerialPort)
            .filter(DeviceSerialPort.device_id == device_id)
            .all()
        ):
            if old.slot not in seen:
                session.query(SerialAction).filter(SerialAction.port_id == old.id).delete()
                session.delete(old)

    audit.record(
        session,
        actor=actor,
        action="serial_config",
        device_id=device_id,
        detail={"slots": sorted(seen), "replace": replace},
    )
    session.commit()
    return {"device_id": device_id, "ports": list_ports(session, device_id)}


def _upsert_action(session: Session, port: DeviceSerialPort, raw: dict) -> None:
    key = str(raw.get("action_key") or "").strip()
    if not key:
        raise DomainError("SERIAL_CONFIG", f"slot {port.slot} 有动作缺 action_key")
    kind = str(raw.get("kind") or "").strip()
    if not kind:
        kind = "power" if key.startswith("power_") else ("mode" if key.startswith("mode_") else "custom")
    if kind not in ("power", "mode", "custom"):
        raise DomainError("SERIAL_CONFIG", f"动作 {key} 的 kind 非法: {kind}")
    try:
        steps = serial_tcp.normalize_steps(raw.get("steps"))
    except serial_tcp.SerialError as e:
        raise DomainError("SERIAL_CONFIG", f"动作 {key}: {e.message}") from e

    act = (
        session.query(SerialAction)
        .filter(SerialAction.port_id == port.id, SerialAction.action_key == key)
        .first()
    )
    if act is None:
        act = SerialAction(port_id=port.id, action_key=key)
        session.add(act)
    act.display_name = (str(raw.get("display_name") or "").strip() or None)
    act.kind = kind
    act.steps = steps
    act.inter_step_ms = max(0, int(raw.get("inter_step_ms") or 0))
    act.enabled = bool(raw.get("enabled", True))
    act.sort = int(raw.get("sort") or 0)


def delete_port(session: Session, device_id: str, slot: int, *, actor: str) -> dict:
    _device(session, device_id)
    port = (
        session.query(DeviceSerialPort)
        .filter(DeviceSerialPort.device_id == device_id, DeviceSerialPort.slot == slot)
        .first()
    )
    if port is None:
        raise DomainError("NOT_FOUND", f"{device_id} 没有串口 slot {slot}")
    session.query(SerialAction).filter(SerialAction.port_id == port.id).delete()
    session.delete(port)
    audit.record(
        session, actor=actor, action="serial_config", device_id=device_id,
        detail={"deleted_slot": slot},
    )
    session.commit()
    return {"device_id": device_id, "deleted_slot": slot}


# ---------------------------------------------------------------- 能力声明

def _state_dict(state: SerialState | None) -> dict:
    if state is None:
        return {
            "power_state": "unknown",
            "mode_state": "unknown",
            "last_action": None,
            "last_result": None,
            "last_by": None,
            "last_at": None,
        }
    return {
        "power_state": state.power_state,
        "mode_state": state.mode_state,
        "last_action": state.last_action,
        "last_result": state.last_result,
        "last_by": state.last_by,
        "last_at": state.last_at.isoformat() if state.last_at else None,
    }


def caps(session: Session, device_id: str) -> dict:
    """能力声明：AI/skill 拿到这份就知道"能不能断电、怎么发、现在谁占着"。"""
    ports = list_ports(session, device_id)
    state = session.get(SerialState, device_id)

    log_port = next((p for p in ports if p["role"] == "log" and p["enabled"]), None)
    power_ports = [p for p in ports if p["role"] == "power" and p["enabled"]]
    mode_ports = [p for p in ports if p["role"] == "mode" and p["enabled"]]

    power_keys: list[str] = []
    mode_keys: list[str] = []
    for p in power_ports:
        for a in p["actions"]:
            if a["action_key"] in POWER_KEYS or a["kind"] == "power":
                if a["action_key"] not in power_keys:
                    power_keys.append(a["action_key"])
    for p in mode_ports:
        for a in p["actions"]:
            if a["action_key"] in MODE_KEYS or a["kind"] == "mode":
                if a["action_key"] not in mode_keys:
                    mode_keys.append(a["action_key"])

    busy = None
    for p in ports:
        key = f"tcp:{(p.get('endpoint') or '').strip().lower()}"
        if p["transport"] == "local":
            key = f"local:{p.get('port_name') or ('slot%s' % p['slot'])}"
        h = serial_tcp.holder_of(key)
        if h:
            busy = {"slot": p["slot"], "role": p["role"], **h}
            break

    return {
        "device_id": device_id,
        "ports": ports,
        "log": (
            {
                "available": True,
                "slot": log_port["slot"],
                "channel": log_port.get("channel"),
                "endpoint": log_port.get("endpoint"),
                "note": "log 串口由 tres-logs 独占采集，TRES 只读其接口/文件，不双开",
            }
            if log_port
            else {"available": False}
        ),
        "power": {"available": bool(power_keys), "actions": power_keys},
        "mode": {"available": bool(mode_keys), "actions": mode_keys},
        "state": _state_dict(state),
        "busy": busy,
        "power_cycle_hold_ms": POWER_CYCLE_HOLD_MS,
    }


def serial_summary(session: Session, device_id: str) -> dict:
    """给 res_device_info / res_list_devices 用的轻量摘要（不含 steps）。"""
    try:
        c = caps(session, device_id)
    except DomainError:
        return {"available": False}
    return {
        "available": bool(c["ports"]),
        "log": c["log"],
        "power": c["power"],
        "mode": c["mode"],
        "state": c["state"],
        "busy": c["busy"],
    }


# ---------------------------------------------------------------- 执行

def _find_action(
    session: Session, device_id: str, action_key: str
) -> tuple[DeviceSerialPort, SerialAction]:
    expect_role = (
        "power" if action_key.startswith("power_")
        else ("mode" if action_key.startswith("mode_") else None)
    )
    ports = (
        session.query(DeviceSerialPort)
        .filter(DeviceSerialPort.device_id == device_id, DeviceSerialPort.enabled.is_(True))
        .order_by(DeviceSerialPort.slot)
        .all()
    )
    if not ports:
        raise DomainError(
            "SERIAL_NOT_CONFIGURED",
            f"{device_id} 还没配串口（先在设备编辑里添加串口与动作）",
        )
    for p in ports:
        if expect_role and p.role != expect_role:
            continue
        act = (
            session.query(SerialAction)
            .filter(
                SerialAction.port_id == p.id,
                SerialAction.action_key == action_key,
                SerialAction.enabled.is_(True),
            )
            .first()
        )
        if act is not None:
            return p, act
    # power_cycle 允许由 power_off + power_on 组合推出
    if action_key == "power_cycle":
        p = next((x for x in ports if x.role == "power"), None)
        if p is not None:
            return p, SerialAction(port_id=p.id, action_key="power_cycle", steps=None)
    raise DomainError(
        "SERIAL_ACTION_NOT_FOUND",
        f"{device_id} 没有定义动作 {action_key}",
        {"available_actions": sorted(_all_action_keys(session, device_id))},
    )


def _all_action_keys(session: Session, device_id: str) -> set[str]:
    ports = session.query(DeviceSerialPort).filter(DeviceSerialPort.device_id == device_id).all()
    if not ports:
        return set()
    rows = (
        session.query(SerialAction)
        .filter(SerialAction.port_id.in_([p.id for p in ports]), SerialAction.enabled.is_(True))
        .all()
    )
    return {r.action_key for r in rows}


def _write_state(
    session: Session,
    device_id: str,
    action_key: str,
    result: str,
    by: str,
) -> None:
    st = session.get(SerialState, device_id)
    if st is None:
        st = SerialState(device_id=device_id)
        session.add(st)
    if result == "ok":
        if action_key in _POWER_STATE_BY_KEY:
            st.power_state = _POWER_STATE_BY_KEY[action_key]
        if action_key in _MODE_STATE_BY_KEY:
            st.mode_state = _MODE_STATE_BY_KEY[action_key]
    elif action_key in _POWER_STATE_BY_KEY:
        st.power_state = "unknown"
    st.last_action = action_key
    st.last_result = result
    st.last_by = by
    st.last_at = utcnow()


def _exec_steps(
    port: DeviceSerialPort,
    steps,
    *,
    hard_timeout_s: float = DEFAULT_HARD_TIMEOUT_S,
) -> serial_tcp.ActionResult:
    return serial_tcp.execute_steps(
        _port_target(port),
        steps,
        default_encoding=port.encoding_default or "hex",
        baudrate=port.baudrate or 115200,
        hard_timeout_s=hard_timeout_s,
    )


def run_action(
    session: Session,
    device_id: str,
    action_key: str,
    *,
    by: str,
    lease_id: str | None = None,
    ttl_sec: float = DEFAULT_TTL_SEC,
    hold_ms: int | None = None,
    hard_timeout_s: float = DEFAULT_HARD_TIMEOUT_S,
    dry_run: bool = False,
) -> dict:
    """服务端执行一个已定义的动作（含 power_cycle 的 off/hold/on 组合）。"""
    _device(session, device_id)
    port, act = _find_action(session, device_id, action_key)
    key = _hold_key(port)
    hold = POWER_CYCLE_HOLD_MS if hold_ms is None else max(0, int(hold_ms))

    if dry_run:
        planned = [s for s in (act.steps or [])]
        return {
            "device_id": device_id,
            "action_key": action_key,
            "port": _port_dict(port),
            "steps": planned,
            "dry_run": True,
            "hold_ms": hold if action_key == "power_cycle" else 0,
        }

    t_all = time.monotonic()
    acquired = False
    try:
        serial_tcp.acquire(key, by, ttl_sec)
        acquired = True
        if action_key == "power_cycle" and not act.steps:
            # 组合动作：power_off -> 保持 -> power_on
            off_act = (
                session.query(SerialAction)
                .filter(SerialAction.port_id == port.id, SerialAction.action_key == "power_off",
                        SerialAction.enabled.is_(True))
                .first()
            )
            on_act = (
                session.query(SerialAction)
                .filter(SerialAction.port_id == port.id, SerialAction.action_key == "power_on",
                        SerialAction.enabled.is_(True))
                .first()
            )
            if off_act is None or on_act is None:
                raise DomainError(
                    "SERIAL_ACTION_NOT_FOUND",
                    "power_cycle 未定义，且缺少 power_off / power_on，无法组合执行",
                )
            r_off = _exec_steps(port, off_act.steps, hard_timeout_s=hard_timeout_s)
            if not r_off.ok:
                result = {
                    "device_id": device_id, "action_key": action_key,
                    "result": "fail", "phase": "power_off",
                    "detail": r_off.to_dict(),
                    "duration_ms": int((time.monotonic() - t_all) * 1000),
                }
                _write_state(session, device_id, action_key, "fail", by)
                _audit(session, device_id, action_key, port, lease_id, by, result)
                session.commit()
                return result
            time.sleep(hold / 1000.0)
            r_on = _exec_steps(port, on_act.steps, hard_timeout_s=hard_timeout_s)
            if not r_on.ok:
                result = {
                    "device_id": device_id, "action_key": action_key,
                    "result": "fail", "phase": "power_on",
                    "detail": r_on.to_dict(), "hold_ms": hold,
                    "duration_ms": int((time.monotonic() - t_all) * 1000),
                }
                _write_state(session, device_id, action_key, "fail", by)
                _audit(session, device_id, action_key, port, lease_id, by, result)
                session.commit()
                return result
            result = {
                "device_id": device_id, "action_key": action_key,
                "result": "ok", "hold_ms": hold,
                "steps": [s.to_dict() for s in r_off.steps] + [s.to_dict() for s in r_on.steps],
                "response_tail": (r_off.response_tail + r_on.response_tail)[-512:],
                "duration_ms": int((time.monotonic() - t_all) * 1000),
            }
            _write_state(session, device_id, action_key, "ok", by)
            _audit(session, device_id, action_key, port, lease_id, by, result)
            session.commit()
            return result

        r = _exec_steps(port, act.steps or [], hard_timeout_s=hard_timeout_s)
        out = r.to_dict()
        result = {
            "device_id": device_id,
            "action_key": action_key,
            "port_slot": port.slot,
            "port_role": port.role,
            "transport": r.transport,
            "target": r.target,
            "result": "ok" if r.ok else "fail",
            "matched": all(s["matched"] for s in out["steps"]) if out["steps"] else False,
            "steps": out["steps"],
            "response_tail": out["response_tail"],
            "duration_ms": int((time.monotonic() - t_all) * 1000),
            "error": out["error"],
            "error_code": out["error_code"],
        }
        _write_state(session, device_id, action_key, result["result"], by)
        _audit(session, device_id, action_key, port, lease_id, by, result)
        session.commit()
        return result
    finally:
        if acquired:
            serial_tcp.release(key, by)


def _audit(
    session: Session,
    device_id: str,
    action_key: str,
    port: DeviceSerialPort,
    lease_id: str | None,
    by: str,
    result: dict,
) -> None:
    audit.record(
        session,
        actor=by,
        action="serial_action",
        device_id=device_id,
        lease_id=lease_id,
        detail={
            "action_key": action_key,
            "port_id": port.id,
            "slot": port.slot,
            "role": port.role,
            "transport": port.transport,
            "endpoint": port.endpoint or port.port_name,
            "steps": len(result.get("steps") or []),
            "matched": result.get("matched"),
            "result": result.get("result"),
            "duration_ms": result.get("duration_ms"),
            "error": result.get("error"),
            "by": by,
        },
    )


# ---------------------------------------------------------------- 读 / 自检 / 上报

def read_serial(
    session: Session,
    device_id: str,
    *,
    slot: int | None = None,
    seconds: float = 3.0,
    max_bytes: int = 8192,
    by: str,
    ttl_sec: float = DEFAULT_TTL_SEC,
) -> dict:
    _device(session, device_id)
    ports = (
        session.query(DeviceSerialPort)
        .filter(DeviceSerialPort.device_id == device_id, DeviceSerialPort.enabled.is_(True))
        .order_by(DeviceSerialPort.slot)
        .all()
    )
    if slot is not None:
        ports = [p for p in ports if p.slot == slot]
    if not ports:
        raise DomainError("SERIAL_NOT_CONFIGURED", f"{device_id} 没有可用串口")
    # 默认优先读 log 口，其次第一个
    port = next((p for p in ports if p.role == "log"), ports[0])

    key = _hold_key(port)
    serial_tcp.acquire(key, by, ttl_sec)
    try:
        r = serial_tcp.read_stream(
            _port_target(port), seconds=seconds, max_bytes=max_bytes,
            baudrate=port.baudrate or 115200,
        )
    finally:
        serial_tcp.release(key, by)

    if port.role == "log":
        cur = session.get(SerialLogCursor, {"device_id": device_id, "port_id": port.id})
        if cur is None:
            cur = SerialLogCursor(device_id=device_id, port_id=port.id, last_offset=0)
            session.add(cur)
        cur.last_offset = (cur.last_offset or 0) + int(r.get("bytes") or 0)
        cur.last_read_at = utcnow()
        session.commit()

    r.update({"device_id": device_id, "slot": port.slot, "role": port.role})
    return r


def probe_port(
    session: Session,
    device_id: str,
    *,
    slot: int | None = None,
    by: str,
    seconds: float = 2.0,
) -> dict:
    _device(session, device_id)
    ports = (
        session.query(DeviceSerialPort)
        .filter(DeviceSerialPort.device_id == device_id)
        .order_by(DeviceSerialPort.slot)
        .all()
    )
    if slot is not None:
        ports = [p for p in ports if p.slot == slot]
    if not ports:
        raise DomainError("SERIAL_NOT_CONFIGURED", f"{device_id} 没有配串口")
    results = []
    for p in ports:
        key = _hold_key(p)
        holder = serial_tcp.holder_of(key)
        if holder:
            results.append({
                "slot": p.slot, "role": p.role, "connected": False,
                "busy": holder, "had_bytes": False, "error": "端口被占用，未探测",
            })
            continue
        serial_tcp.acquire(key, by, 30)
        try:
            r = serial_tcp.probe_target(
                _port_target(p), baudrate=p.baudrate or 115200, seconds=seconds
            )
        finally:
            serial_tcp.release(key, by)
        results.append({
            "slot": p.slot, "role": p.role,
            "endpoint": p.endpoint or p.port_name,
            "channel": p.channel,
            "connected": r.get("connected"),
            "had_bytes": r.get("had_bytes"),
            "bytes": r.get("bytes"),
            "error": r.get("error"),
        })
    return {"device_id": device_id, "ports": results}


def report_result(
    session: Session,
    device_id: str,
    action_key: str,
    result: str,
    *,
    by: str,
    detail: dict | None = None,
) -> dict:
    """skill 直连 TCP 执行后回填状态与审计（by 必须非空，防匿名伪造）。"""
    _device(session, device_id)
    if not by:
        raise DomainError("AGENT_INFO_REQUIRED", "上报必须带 by（agent_info）")
    if action_key not in _all_action_keys(session, device_id):
        raise DomainError(
            "SERIAL_ACTION_NOT_FOUND",
            f"{device_id} 没有定义动作 {action_key}，不接受上报",
        )
    normalized = "ok" if str(result).lower() in ("ok", "success", "true") else "fail"
    _write_state(session, device_id, action_key, normalized, by)
    audit.record(
        session,
        actor=by,
        action="serial_report",
        device_id=device_id,
        detail={"action_key": action_key, "result": normalized, **(detail or {})},
    )
    session.commit()
    state = session.get(SerialState, device_id)
    return {"device_id": device_id, "action_key": action_key, "result": normalized,
            "state": _state_dict(state)}


# ---------------------------------------------------------------- 权限 / 列表标记

def check_access(
    session: Session,
    device_id: str,
    *,
    email: str,
    is_admin: bool,
    action_key: str | None = None,
    confirm: bool = False,
) -> None:
    """执行类操作的权限：admin 或该设备活跃租约持有人；EDL 额外要求 admin + confirm。"""
    from app.constants import LeaseState
    from app.models import Lease

    if action_key in ("mode_edl",):
        if not is_admin:
            raise DomainError(
                "FORBIDDEN", "mode_edl 只允许管理员执行（进 EDL 后设备不再响应）"
            )
        if not confirm:
            raise DomainError(
                "CONFIRM_REQUIRED", "mode_edl 需要 confirm=true 二次确认",
                {"action_key": action_key},
            )
    if is_admin:
        return
    lease = (
        session.query(Lease)
        .filter(Lease.device_id == device_id, Lease.state.in_(LeaseState.ACTIVE))
        .first()
    )
    holder = lease.principal.email if (lease and lease.principal) else None
    if lease is None:
        raise DomainError(
            "NO_LEASE",
            f"当前没有 {device_id} 的活跃租约；串口动作要求先 res_apply 拿到租约",
        )
    if holder and holder != email:
        raise DomainError(
            "FORBIDDEN",
            f"{device_id} 当前租约持有人是 {holder}，只有持有人或管理员能操作串口",
            {"holder": holder},
        )


def serial_flags(session: Session) -> dict[str, dict]:
    """一次查询算出所有设备的串口能力标记（供 res_list_devices 附在每条上）。"""
    ports = session.query(DeviceSerialPort).filter(DeviceSerialPort.enabled.is_(True)).all()
    if not ports:
        return {}
    acts = (
        session.query(SerialAction)
        .filter(
            SerialAction.port_id.in_([p.id for p in ports]),
            SerialAction.enabled.is_(True),
        )
        .all()
    )
    keys_by_port: dict[int, set[str]] = {}
    for a in acts:
        keys_by_port.setdefault(a.port_id, set()).add(a.action_key)

    out: dict[str, dict] = {}
    for p in ports:
        f = out.setdefault(
            p.device_id, {"log": False, "power": False, "mode": False, "slots": 0}
        )
        f["slots"] += 1
        if p.role == "log":
            f["log"] = True
        elif p.role == "power" and (keys_by_port.get(p.id) or set()) & set(POWER_KEYS):
            f["power"] = True
        elif p.role == "mode" and (keys_by_port.get(p.id) or set()) & set(MODE_KEYS):
            f["mode"] = True
    return out


def reset_state(session: Session, device_id: str, *, actor: str) -> dict:
    """admin 手工把状态归零（例如换了电源板后不想带着旧的 on/off 判断）。"""
    st = session.get(SerialState, device_id)
    if st is None:
        st = SerialState(device_id=device_id)
        session.add(st)
    st.power_state = "unknown"
    st.mode_state = "unknown"
    st.last_action = None
    st.last_result = "reset"
    st.last_by = actor
    st.last_at = utcnow()
    session.commit()
    return _state_dict(st)

