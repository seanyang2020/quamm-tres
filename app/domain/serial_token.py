"""租约级串口派生票（serialhub 1.1.0 方案④，TRES 侧配套）。

契约（与 serialhub `src/tokens.c` / `src/mcp.c` 对齐）：
- 派生票由 TRES 随机生成 ``rt_<32hex>``；serialhub 只存 ``sha256(token)`` 摘要，不落明文。
- 写入经主 token 调 serialhub MCP ``token_add``，``label=lease:<lease_id>``；
  释放 / 换租约 / 心跳超时回收时按同一 label 调 ``token_del``，**即时撤销**，无需重启。
- 同一 label 重复 ``token_add`` 会覆盖旧摘要 → 重签自然作废旧票，撤销口径始终是 label。
- 主 token 只存在于 ``wl_node.caps_json["local_token"]``（TRES 管理通道凭据），
  **永不出现在任何返回给 agent / 用户的响应或日志里**。
- 日志机不可达 / 未上报主 token 时写失败 → ``lease.serial_grant="pending"``，worker 周期重试。

端口掩码陷阱：serialhub 收到 ``ports`` 里无效路号会算不出 mask 而退化成 ``0=全部路``
（``tokens.c: ports_mask=0 → token_allows_port 放行全部``）。所以这里**只传 1..HUB_MAX_PORTS
范围内的 slot**，全部越界则拒绝签发，绝不让一张票变成全权限。
"""
from __future__ import annotations

import json
import logging
import secrets
import socket
import urllib.error
import urllib.request
from datetime import timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.constants import LeaseState
from app.domain import audit
from app.domain.catalog import DomainError
from app.models import Device, Lease, WireLogNode, WireLogPortBinding
from app.utils import utcnow

logger = logging.getLogger("quamm-tres.serial_token")

# serialhub 侧常量（src/hub.h: HUB_MAX_PORTS 6 / 默认 MCP 8787、raw TCP base 9000）
HUB_MAX_PORTS = 6
DEFAULT_MCP_PORT = 8787
# 注意：caps.raw_tcp_port 是**第 1 路的端口本身**（serialhub 的 raw_tcp_base+1），
# 与 TRES 既有口径一致：port = raw_tcp_port + (slot-1)。
DEFAULT_FIRST_PORT = 9001
TOKEN_PREFIX = "rt_"

GRANT_NONE = "none"          # 未涉及串口（设备未绑定日志机）
GRANT_PENDING = "pending"    # 需要签发但当前写不进去，等 worker 重试
GRANT_GRANTED = "granted"    # 已写入 serialhub，租约期内有效

# 派生票在 serialhub 侧的可用工具（与 src/mcp.c ADMIN_TOOLS 黑名单互补）
DERIVED_SCOPES = [
    "serial_list", "serial_takeover", "serial_takeover_write", "serial_release",
    "log_query", "log_tail", "log_files", "log_stream_info",
    "node_get_info", "node_status", "calib_hotplug_events",
    "patrol_report", "ticket_list", "log_config_get",
]


# ------------------------------------------------------------------ 基础工具

def lease_label(lease_id: str) -> str:
    """派生票标签。撤销只认 label，票值可重签。"""
    return f"lease:{lease_id}"


def _caps(node: WireLogNode) -> dict:
    """caps_json 在 SQLAlchemy 里是 dict，旧库直读可能是 str（兼容）。"""
    raw = node.caps_json
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _master_token(node: WireLogNode) -> str:
    """日志机主 token：只供 TRES 内部管理通道使用，任何返回值都不带它。"""
    return str(_caps(node).get("local_token") or "").strip()


def _first_port(node: WireLogNode) -> int:
    """第 1 路 raw TCP 端口（caps.raw_tcp_port）。"""
    try:
        return int(_caps(node).get("raw_tcp_port") or DEFAULT_FIRST_PORT)
    except (TypeError, ValueError):
        return DEFAULT_FIRST_PORT


def _raw_tcp_for(node: WireLogNode, slot: int) -> str | None:
    """路号 → raw TCP 端点。第 1 路就是 raw_tcp_port，之后按序 +1。"""
    if not node.ip:
        return None
    return f"{node.ip}:{_first_port(node) + (slot - 1)}"


def _exp_ms(lease: Lease) -> int:
    """租约到期时刻（epoch ms）；0=永不过期（这里不用，过期兜底必须存在）。"""
    if lease.expires_at is None:
        return 0
    dt = lease.expires_at
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _new_derived_token() -> str:
    return TOKEN_PREFIX + secrets.token_hex(16)


# ------------------------------------------------------------------ serialhub MCP

def _mcp_call(node: WireLogNode, tool: str, args: dict, *, timeout: float | None = None) -> dict:
    """调日志机 serialhub MCP。返回 {"ok": bool, "result": dict|None, "error": str|None}。"""
    ip = (node.ip or "").strip()
    if not ip:
        return {"ok": False, "result": None, "error": "日志机未上报 IP（等待心跳）"}
    token = _master_token(node)
    if not token:
        return {"ok": False, "result": None, "error": "日志机未上报主 token（等待心跳）"}

    url = f"http://{ip}:{DEFAULT_MCP_PORT}/mcp"
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": "Bearer " + token,
        },
    )
    to = timeout if timeout is not None else settings.serial_token_mcp_timeout
    try:
        with urllib.request.urlopen(req, timeout=to) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        return {"ok": False, "result": None, "error": f"日志机不可达({exc})"}

    parsed = None
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except ValueError:
                continue
    if not isinstance(parsed, dict):
        return {"ok": False, "result": None, "error": f"响应非 JSON: {raw[:120]}"}
    if parsed.get("error"):
        return {"ok": False, "result": None, "error": str(parsed["error"])[:160]}

    result = parsed.get("result") or {}
    if result.get("isError"):
        text = "".join(c.get("text", "") for c in (result.get("content") or []))
        return {"ok": False, "result": None, "error": text[:160] or "工具返回错误"}
    content = result.get("content") or []
    text = "".join(c.get("text", "") for c in content)
    try:
        return {"ok": True, "result": json.loads(text) if text else {}, "error": None}
    except ValueError:
        return {"ok": True, "result": {"text": text}, "error": None}


# ------------------------------------------------------------------ 绑定解析

def _bindings_for(session: Session, device_id: str) -> list[tuple[WireLogNode, int]]:
    """设备 → [(日志机, slot)]。slot 必须 1..HUB_MAX_PORTS（越界票会退化成全权限）。"""
    rows = (
        session.query(WireLogPortBinding)
        .filter(WireLogPortBinding.bound_device_id == device_id)
        .all()
    )
    out: list[tuple[WireLogNode, int]] = []
    for b in rows:
        slot = int(b.slot or 0)
        if slot < 1 or slot > HUB_MAX_PORTS:
            logger.warning(
                "serial token: skip out-of-range slot %s on node %s (device %s)",
                b.slot, b.node_id, device_id,
            )
            continue
        node = session.get(WireLogNode, b.node_id)
        if node is None:
            continue
        out.append((node, slot))
    return out


def bindings_summary(session: Session, device_id: str) -> list[dict]:
    """给 caps/工具展示用：这台设备挂在哪台日志机的哪一路。"""
    return [
        {
            "node_id": node.node_id,
            "display_name": node.display_name,
            "ip": node.ip,
            "slot": slot,
            "raw_tcp": _raw_tcp_for(node, slot),
        }
        for node, slot in _bindings_for(session, device_id)
    ]


# ------------------------------------------------------------------ 签发 / 撤销

def _set_grant(lease: Lease, state: str, detail: dict) -> None:
    lease.serial_grant = state
    lease.serial_token_label = lease_label(lease.lease_id) if state != GRANT_NONE else None
    lease.serial_grant_detail = detail
    lease.serial_grant_at = utcnow()


def provision(
    session: Session,
    lease: Lease,
    device: Device | None = None,
    *,
    actor: str = "system",
    want_plain: bool = False,
) -> dict:
    """签发（或重签）本租约的串口派生票。

    - 同一 label 覆盖写入：重签后旧票立即失效（serialhub token_add 按 label 更新摘要）。
    - ``want_plain=True`` 时返回票值明文（只在 MCP 响应里出现一次，不落库）；
      内部重试路径永远不取明文。
    - 失败不抛异常：标 ``pending`` 由 worker 重试，**不让串口签发拖垮申请/释放主流程**。
    """
    device = device or session.get(Device, lease.device_id)
    bindings = _bindings_for(session, lease.device_id)
    label = lease_label(lease.lease_id)

    if not bindings:
        _set_grant(lease, GRANT_NONE, {"reason": "设备未绑定日志机串口"})
        session.flush()
        return {"granted": False, "reason": "设备未绑定日志机串口", "label": label}

    exp_ms = _exp_ms(lease)
    token = _new_derived_token()
    nodes: dict[str, dict] = {}
    failures: list[str] = []

    # 按日志机分组：同一票值写进每台日志机（各家表独立，但摘要同一，agent 一票通用）
    grouped: dict[str, tuple[WireLogNode, list[int]]] = {}
    for node, slot in bindings:
        if node.node_id not in grouped:
            grouped[node.node_id] = (node, [])
        grouped[node.node_id][1].append(slot)

    for node_id, (node, slots) in grouped.items():
        res = _mcp_call(node, "token_add", {
            "token": token,
            "label": label,
            "exp_ms": exp_ms,
            "ports": sorted(set(slots)),
            "admin": False,
        })
        nodes[node_id] = {
            "slots": sorted(set(slots)),
            "ok": bool(res["ok"]),
            "error": res["error"],
            "raw_tcp": _raw_tcp_for(node, min(slots)),
            "mcp_url": f"http://{node.ip}:{DEFAULT_MCP_PORT}/mcp" if node.ip else None,
        }
        if not res["ok"]:
            failures.append(f"{node_id}: {res['error']}")

    if failures:
        _set_grant(lease, GRANT_PENDING, {"label": label, "nodes": nodes, "error": "; ".join(failures)[:400]})
        audit.record(
            session, actor=actor, action="serial_token_pending",
            device_id=lease.device_id, lease_id=lease.lease_id,
            detail={"label": label, "errors": failures[:5]},
        )
        session.flush()
        logger.warning("serial token pending for lease %s: %s", lease.lease_id, "; ".join(failures))
        return {"granted": False, "pending": True, "label": label, "errors": failures}

    _set_grant(lease, GRANT_GRANTED, {"label": label, "nodes": nodes, "exp_ms": exp_ms})
    audit.record(
        session, actor=actor, action="serial_token_issued",
        device_id=lease.device_id, lease_id=lease.lease_id,
        detail={"label": label, "nodes": {k: v["slots"] for k, v in nodes.items()}},
    )
    session.flush()

    out: dict = {"granted": True, "label": label, "exp_ms": exp_ms, "nodes": nodes}
    if want_plain:
        out["token"] = token
    return out


def revoke(
    session: Session,
    lease: Lease,
    *,
    actor: str = "system",
    reason: str = "lease_closed",
) -> dict:
    """按 label 撤销派生票（幂等：票不存在也返回 ok）。best-effort，不抛异常。"""
    label = lease.serial_token_label or lease_label(lease.lease_id)
    try:
        bindings = _bindings_for(session, lease.device_id)
    except Exception:  # noqa: BLE001
        bindings = []

    nodes: dict[str, dict] = {}
    for node, _slot in bindings:
        if node.node_id in nodes:
            continue
        res = _mcp_call(node, "token_del", {"label": label})
        nodes[node.node_id] = {"ok": bool(res["ok"]), "error": res["error"]}
        if not res["ok"]:
            logger.warning("serial token revoke failed on %s for %s: %s", node.node_id, label, res["error"])

    audit.record(
        session, actor=actor, action="serial_token_revoked",
        device_id=lease.device_id, lease_id=lease.lease_id,
        detail={"label": label, "reason": reason, "nodes": nodes},
    )
    if lease.serial_grant not in (None, GRANT_NONE):
        lease.serial_grant = GRANT_NONE
    lease.serial_token_label = None
    session.flush()
    return {"label": label, "revoked": all(v["ok"] for v in nodes.values()) if nodes else True, "nodes": nodes}


# ------------------------------------------------------------------ 对外（MCP）

def token_for_lease(session: Session, auth, lease_id: str) -> dict:
    """agent 取本租约的串口派生票（每次调用都重签，旧票作废）。

    返回体只含派生票，**不含主 token**。
    """
    lease = session.get(Lease, lease_id)
    if lease is None:
        raise DomainError("NOT_FOUND", f"lease {lease_id} not found")
    if lease.state not in LeaseState.ACTIVE:
        raise DomainError("LEASE_NOT_ACTIVE", f"租约 {lease_id} 当前状态 {lease.state}，不可取串口票")
    principal = getattr(auth, "principal", None)
    if principal is None:
        raise DomainError("FORBIDDEN", "缺少调用方身份")
    if lease.principal_id != principal.id and not getattr(auth, "is_admin", False):
        raise DomainError("FORBIDDEN", "只能取自己租约的串口票")

    device = session.get(Device, lease.device_id)
    res = provision(
        session, lease, device,
        actor=getattr(auth, "email", "agent"), want_plain=True,
    )
    session.commit()

    if not res.get("granted"):
        if res.get("pending"):
            raise DomainError(
                "SERIAL_TOKEN_PENDING",
                "日志机暂不可达/未上报主 token，派生票已排队重试；稍后再调本工具",
                {"extra": {"label": res.get("label"), "errors": res.get("errors")}},
            )
        raise DomainError("SERIAL_NOT_BOUND", res.get("reason", "设备未绑定日志机串口"))

    node_list = [
        {
            "node_id": nid,
            "raw_tcp": v["raw_tcp"],
            "mcp_url": v["mcp_url"],
            "slots": v["slots"],
        }
        for nid, v in res["nodes"].items()
    ]
    return {
        "lease_id": lease_id,
        "device_id": lease.device_id,
        "label": res["label"],
        "token_type": "derived",
        "token": res["token"],
        "expires_at": lease.expires_at.isoformat() if lease.expires_at else None,
        "ports": sorted({s for v in res["nodes"].values() for s in v["slots"]}),
        "access": node_list,
        "login_hint": "raw TCP：连上收到 login: 后发 <token> + 换行(LF)；MCP：Authorization: Bearer <token>",
        "scopes": DERIVED_SCOPES,
        "note": "受限票：只读 + 本路串口读写；管理面工具被拒。租约释放/回收后立即失效，重取会作废旧票。",
    }


# ------------------------------------------------------------------ worker 重试

def retry_pending(session: Session, limit: int = 20) -> int:
    """worker：把 pending 的活跃租约再签一次。返回到达 granted 的条数。"""
    leases = (
        session.query(Lease)
        .filter(
            Lease.state.in_(list(LeaseState.ACTIVE)),
            Lease.serial_grant == GRANT_PENDING,
        )
        .order_by(Lease.serial_grant_at)
        .limit(limit)
        .all()
    )
    done = 0
    for lease in leases:
        try:
            res = provision(session, lease, actor="worker")
            if res.get("granted"):
                done += 1
        except Exception:  # noqa: BLE001
            logger.exception("serial token retry failed for %s", lease.lease_id)
    if done:
        session.commit()
    return done
