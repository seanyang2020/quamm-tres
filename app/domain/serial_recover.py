"""串口恢复：ADB 不通时，通过同一设备的 TCP COM 执行排障/重启。

三级能力（用户已拍板全部允许）：
- readonly：只读排障（id / 机型 / adbd 状态 / dmesg 尾部）
- restart_adbd：重启 adbd（不动系统）
- reboot：整机重启

执行路径：节点 serialhub MCP（takeover 写锁 → serial_takeover_write → tail 回读 → release），
认证用节点上报的 caps.local_token（X-Auth-Token），超时/异常都会释放写锁。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
import uuid

from sqlalchemy.orm import Session

from app.domain import audit, bundle as bundle_svc, serial as serial_svc
from app.domain.catalog import DomainError
from app.models import WireLogNode

LEVELS = {
    "readonly": {
        "label": "只读排障",
        "commands": [
            "id",
            "uname -a",
            "cat /etc/build-info",
            "dmesg | tail -20",
        ],
    },
    "restart_adbd": {
        "label": "重启 adbd",
        "commands": [
            "ps | grep adbd | grep -v grep",
            "killall adbd 2>/dev/null; sleep 1; "
            "[ -x /sbin/adbd ] && setsid /sbin/adbd -o /dev/null; "
            "[ -x /system/bin/adbd ] && setsid /system/bin/adbd; "
            "sleep 2; ps | grep adbd | grep -v grep",
        ],
    },
    "reboot": {
        "label": "整机重启",
        "commands": ["reboot"],
    },
}

_OWNER = "tres-recover"


def _sentinel() -> str:
    """每次恢复用随机哨兵，避免 tail 里的历史标记造成假成功。"""
    return f"__TRES_RECOVER_DONE_{uuid.uuid4().hex[:10]}__"


def _http_json(url: str, token: str, payload: dict | None = None,
               timeout: float = 10.0) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"X-Auth-Token": token, "Content-Type": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - 内网节点
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        raise DomainError("SERIAL_BAD_RESPONSE", f"节点返回非 JSON: {raw[:160]}")


def _mcp(base: str, token: str, name: str, args: dict) -> dict:
    body = {
        "jsonrpc": "2.0", "id": int(time.time() * 1000),
        "method": "tools/call", "params": {"name": name, "arguments": args},
    }
    resp = _http_json(base + "/mcp", token, body)
    if "error" in resp:
        raise DomainError("SERIAL_MCP_ERROR", str(resp["error"]))
    result = resp.get("result") or {}
    text = ""
    content = result.get("content") or []
    if content:
        text = content[0].get("text") or ""
    try:
        parsed = json.loads(text) if text else {}
    except json.JSONDecodeError:
        parsed = {"raw": text}
    if result.get("isError"):
        raise DomainError(
            "SERIAL_MCP_FAILED",
            (parsed or {}).get("error") if isinstance(parsed, dict) else text,
        )
    return parsed


def _node_context(session: Session, device_id: str, slot: int | None = None) -> dict:
    b = bundle_svc.device_bundle(session, device_id)
    channels = b["channels"]["serial"]
    if not channels:
        raise DomainError(
            "NO_SERIAL_CHANNEL",
            f"{device_id} 没有绑定串口；无法走串口恢复（先用资源池绑定或等指纹自动匹配）",
        )
    if slot is not None:
        channels = [c for c in channels if c["source"]["slot"] == int(slot)] or channels
    ch = channels[0]
    node_id = ch["source"]["node_id"]
    node = session.get(WireLogNode, node_id)
    token = (node.caps_json or {}).get("local_token") if node else None
    if not token:
        raise DomainError(
            "NO_SERIAL_TOKEN",
            f"节点 {node_id} 没上报 local_token，串口恢复不可用（重启 wl-agent 后重试）",
        )
    host = ch["source"]["node_ip"]
    if not host:
        raise DomainError("NO_NODE_IP", f"节点 {node_id} 没有 IP")
    return {
        "channel": ch,
        "node_id": node_id,
        "base": f"http://{host}:8787",
        "token": token,
        "index": int(ch["source"]["slot"]),
        "endpoint": ch["endpoint"],
    }


def _tail(base: str, token: str, index: int, lines: int = 300) -> str:
    url = f"{base}/api/tail?index={index}&lines={lines}"
    req = urllib.request.Request(url, headers={"X-Auth-Token": token})
    with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310
        return r.read().decode("utf-8", "replace")


def recover_via_serial(
    session: Session,
    *,
    device_id: str,
    level: str,
    actor: str,
    slot: int | None = None,
    wait_timeout: float = 20.0,
) -> dict:
    if level not in LEVELS:
        raise DomainError(
            "BAD_LEVEL",
            f"level 只支持 {', '.join(LEVELS)}（收到 {level}）",
        )
    ctx = _node_context(session, device_id, slot)
    base, token, idx = ctx["base"], ctx["token"], ctx["index"]
    owner = f"{_OWNER}:{device_id}"
    commands = list(LEVELS[level]["commands"])

    # 端口没打开时先尝试打开（失败由 write 报错兜底）
    try:
        _mcp(base, token, "serial_open", {"index": idx})
    except DomainError:
        pass

    out = {
        "ok": False,
        "device_id": device_id,
        "level": level,
        "level_label": LEVELS[level]["label"],
        "channel": {
            "com": ctx["channel"]["com"],
            "endpoint": ctx["endpoint"],
            "node_id": ctx["node_id"],
            "slot": idx,
        },
        "commands": commands,
        "output": "",
    }

    started = time.time()
    try:
        _mcp(base, token, "serial_takeover", {"index": idx, "owner": owner})
        for cmd in commands:
            sent = _mcp(
                base, token, "serial_takeover_write",
                {"index": idx, "owner": owner, "data": cmd},
            )
            if not sent.get("ok"):
                raise DomainError("SERIAL_WRITE_FAILED", f"写入失败: {cmd}")
            time.sleep(0.4)
        if level == "reboot":
            out["ok"] = True
            out["output"] = "reboot 已下发（串口会随设备重启断开，属正常现象）"
            out["duration_ms"] = int((time.time() - started) * 1000)
            return out

        # 用随机哨兵确认命令序列执行完毕（历史 tail 不会误命中）
        sentinel = _sentinel()
        _mcp(base, token, "serial_takeover_write",
             {"index": idx, "owner": owner, "data": f"echo {sentinel}"})
        deadline = time.time() + wait_timeout
        text = ""
        while time.time() < deadline:
            time.sleep(0.6)
            try:
                text = _tail(base, token, idx)
            except Exception:  # noqa: BLE001 - 读日志失败继续等
                continue
            if sentinel in text:
                break
        out["output"] = text[-8000:] if text else ""
        out["ok"] = bool(sentinel in text)
        out["sentinel_seen"] = bool(sentinel in text)
        out["duration_ms"] = int((time.time() - started) * 1000)
        return out
    except urllib.error.URLError as e:
        raise DomainError("SERIAL_UNREACHABLE", f"连不上节点串口控制台: {e}") from e
    finally:
        try:
            _mcp(base, token, "serial_release", {"index": idx, "owner": owner})
        except Exception:  # noqa: BLE001 - 释放失败不掩盖主流程
            pass
        audit.record(
            session,
            actor=actor,
            action=f"serial_recover:{level}",
            device_id=device_id,
            detail={"node_id": ctx["node_id"], "slot": idx,
                    "com": ctx["channel"]["com"], "endpoint": ctx["endpoint"],
                    "ok": out["ok"]},
        )
        session.commit()


def recover(
    session: Session,
    *,
    device_id: str,
    level: str,
    actor: str,
    email: str,
    is_admin: bool,
    slot: int | None = None,
) -> dict:
    """权限同串口动作：管理员或该设备活跃租约持有人。"""
    serial_svc.check_access(
        session, device_id, email=email, is_admin=is_admin,
        action_key=f"serial_recover:{level}",
    )
    return recover_via_serial(
        session, device_id=device_id, level=level, actor=actor, slot=slot,
    )
