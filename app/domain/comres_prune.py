"""COMRes 无效资源审计 / 清理（t942）。

规则（可配，见 Settings）：
- online 心跳超过 max(3*hb, floor) → 节点 offline，端口 health_state=stale
- offline 连续 24h → 删除 binding，节点 dormant
- enrolled 超过 2h 从未心跳 → retired 并清 binding
- reject/revoke → 立即清 binding（revoke 已有；reject 补齐）
- allocated 仅在「节点非 online 且 TTL 到期」时回收
"""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.domain import wirelog
from app.models import WireLogClientApply, WireLogNode, WireLogPortBinding
from app.utils import utcnow


INACTIVE_HEALTH = frozenset({"stale", "superseded", "offline"})


def stale_timeout_s() -> int:
    hb = int(getattr(settings, "comres_heartbeat_interval_s", 3600) or 3600)
    floor = int(getattr(settings, "comres_stale_floor_s", 1800) or 1800)
    return max(3 * hb, floor)


def offline_dormant_s() -> int:
    return int(getattr(settings, "comres_offline_dormant_s", 24 * 3600) or 24 * 3600)


def enrolled_retire_s() -> int:
    return int(getattr(settings, "comres_enrolled_retire_s", 2 * 3600) or 2 * 3600)


def _audit(session, node_id, action, *, result="ok", detail=None, actor=None):
    wirelog._audit(session, node_id, action, actor=actor or "prune",
                   result=result, detail=detail)


def _binding_brief(b: WireLogPortBinding) -> dict:
    return {
        "id": b.id,
        "node_id": b.node_id,
        "com": b.com_name,
        "com_key": b.com_key,
        "endpoint": b.endpoint,
        "health_state": b.health_state,
        "allocated_by": b.allocated_by,
        "allocated_expire": (
            b.allocated_expire.isoformat() if b.allocated_expire else None
        ),
        "last_checked_at": (
            b.last_checked_at.isoformat() if b.last_checked_at else None
        ),
    }


def _node_brief(n: WireLogNode) -> dict:
    return {
        "node_id": n.node_id,
        "display_name": n.display_name,
        "status": n.status,
        "last_heartbeat_at": (
            n.last_heartbeat_at.isoformat() if n.last_heartbeat_at else None
        ),
        "created_at": n.created_at.isoformat() if n.created_at else None,
    }


def find_duplicates(session: Session) -> list[dict]:
    """扫描 (node_id, com) 与跨 node com_key 重复。"""
    rows = session.query(WireLogPortBinding).all()
    out = []
    by_nc: dict[tuple[str, str], list] = {}
    for b in rows:
        com = (b.com_name or "").strip()
        if not com:
            continue
        by_nc.setdefault((b.node_id, com), []).append(b)
    for (node_id, com), group in by_nc.items():
        if len(group) > 1:
            out.append({
                "kind": "node_com",
                "node_id": node_id,
                "com": com,
                "count": len(group),
                "ids": [b.id for b in group],
                "samples": [_binding_brief(b) for b in group[:5]],
            })

    by_ck: dict[str, list] = {}
    for b in rows:
        if not b.com_key:
            continue
        if (b.health_state or "").lower() == "superseded":
            continue
        by_ck.setdefault(b.com_key, []).append(b)
    for com_key, group in by_ck.items():
        nodes = {b.node_id for b in group}
        if len(nodes) > 1:
            out.append({
                "kind": "endpoint",
                "com_key": com_key,
                "count": len(group),
                "node_ids": sorted(nodes),
                "ids": [b.id for b in group],
                "samples": [_binding_brief(b) for b in group[:5]],
            })
    return out


def audit_maintenance(session: Session) -> dict:
    """dry-run：返回 duplicates / stale / orphan_nodes / allocated_zombie 与预计影响条数。"""
    now = utcnow()
    stale_before = now - timedelta(seconds=stale_timeout_s())
    dormant_before = now - timedelta(seconds=offline_dormant_s())
    enroll_before = now - timedelta(seconds=enrolled_retire_s())

    nodes = {n.node_id: n for n in session.query(WireLogNode).all()}
    bindings = session.query(WireLogPortBinding).all()

    duplicates = find_duplicates(session)

    stale = []
    for n in nodes.values():
        if (n.status or "") != "online":
            continue
        if not n.last_heartbeat_at or n.last_heartbeat_at > stale_before:
            continue
        ports = [b for b in bindings if b.node_id == n.node_id]
        stale.append({
            **_node_brief(n),
            "action": "mark_offline_stale",
            "ports": len(ports),
            "samples": [_binding_brief(b) for b in ports[:3]],
        })

    orphan_nodes = []
    for n in nodes.values():
        st = (n.status or "")
        if st == "offline":
            # 以节点 last_heartbeat 为 offline 起点近似；无心跳则用 created
            ref = n.last_heartbeat_at or n.created_at
            if ref and ref <= dormant_before:
                ports = [b for b in bindings if b.node_id == n.node_id]
                orphan_nodes.append({
                    **_node_brief(n),
                    "action": "dormant_purge_bindings",
                    "ports": len(ports),
                    "samples": [_binding_brief(b) for b in ports[:3]],
                })
        elif st == "enrolled" and n.last_heartbeat_at is None:
            ref = n.provisioned_at or n.created_at
            if ref and ref <= enroll_before:
                ports = [b for b in bindings if b.node_id == n.node_id]
                orphan_nodes.append({
                    **_node_brief(n),
                    "action": "retire_never_heartbeat",
                    "ports": len(ports),
                    "samples": [_binding_brief(b) for b in ports[:3]],
                })

    allocated_zombie = []
    for b in bindings:
        if not b.allocated_by:
            continue
        if b.allocated_expire is None or b.allocated_expire > now:
            continue
        node = nodes.get(b.node_id)
        if node is not None and (node.status or "") == "online":
            continue
        allocated_zombie.append({
            **_binding_brief(b),
            "node_status": node.status if node else None,
            "action": "release_allocation",
        })

    return {
        "duplicates": duplicates,
        "stale": stale,
        "orphan_nodes": orphan_nodes,
        "allocated_zombie": allocated_zombie,
        "counts": {
            "duplicates": len(duplicates),
            "stale": len(stale),
            "orphan_nodes": len(orphan_nodes),
            "allocated_zombie": len(allocated_zombie),
            "stale_ports": sum(x.get("ports", 0) for x in stale),
            "orphan_ports": sum(x.get("ports", 0) for x in orphan_nodes),
        },
        "thresholds": {
            "stale_timeout_s": stale_timeout_s(),
            "offline_dormant_s": offline_dormant_s(),
            "enrolled_retire_s": enrolled_retire_s(),
        },
    }


def clear_node_bindings(session: Session, node_id: str, *, actor=None,
                        reason: str = "cleanup") -> int:
    """删除节点全部 binding 并写 audit；返回删除条数。"""
    rows = (session.query(WireLogPortBinding)
            .filter_by(node_id=node_id).all())
    n = len(rows)
    for b in rows:
        session.delete(b)
    if n:
        _audit(session, node_id, "comres_prune_bindings",
               actor=actor, detail={"removed": n, "reason": reason})
    return n


def prune_maintenance(session: Session, *, dry_run: bool = False,
                      actor: str | None = None) -> dict:
    """按规则执行清理；dry_run 时只返回计划。"""
    plan = audit_maintenance(session)
    if dry_run:
        return {"dry_run": True, "ok": True, **plan, "applied": []}

    now = utcnow()
    applied = []
    actor = actor or "prune"

    # 1) online 超时 → offline + ports stale
    for item in plan["stale"]:
        node = session.get(WireLogNode, item["node_id"])
        if node is None:
            continue
        node.status = "offline"
        ports = (session.query(WireLogPortBinding)
                 .filter_by(node_id=node.node_id).all())
        for b in ports:
            if (b.health_state or "").lower() in ("superseded",):
                continue
            b.health_state = "stale"
        _audit(session, node.node_id, "comres_prune_stale",
               actor=actor, detail={
                   "ports": len(ports),
                   "last_heartbeat_at": item.get("last_heartbeat_at"),
               })
        applied.append({"action": "mark_offline_stale", "node_id": node.node_id,
                        "ports": len(ports)})

    # 2) offline 24h → 删 binding + dormant；enrolled 2h → retired + 清 binding
    for item in plan["orphan_nodes"]:
        node = session.get(WireLogNode, item["node_id"])
        if node is None:
            continue
        removed = clear_node_bindings(
            session, node.node_id, actor=actor, reason=item["action"])
        if item["action"] == "dormant_purge_bindings":
            node.status = "dormant"
        else:
            node.status = "retired"
        _audit(session, node.node_id, "comres_prune_orphan",
               actor=actor, detail={
                   "action": item["action"], "bindings_removed": removed,
               })
        applied.append({
            "action": item["action"], "node_id": node.node_id,
            "bindings_removed": removed, "new_status": node.status,
        })

    # 3) allocated zombie：节点非 online 且 TTL 到期 → 清占用
    # 在 stale 落库之后重扫，避免「本轮刚转 offline」的幽灵占用漏到下一小时
    session.flush()
    nodes_now = {n.node_id: n for n in session.query(WireLogNode).all()}
    for b in session.query(WireLogPortBinding).all():
        if not b.allocated_by:
            continue
        if b.allocated_expire is not None and b.allocated_expire > now:
            continue
        node = nodes_now.get(b.node_id)
        if node is not None and (node.status or "") == "online":
            continue
        detail = {
            "com": b.com_name, "com_key": b.com_key,
            "was_allocated_by": b.allocated_by,
            "allocated_expire": (
                b.allocated_expire.isoformat() if b.allocated_expire else None
            ),
        }
        b.allocated_by = None
        b.allocated_at = None
        b.allocated_expire = None
        _audit(session, b.node_id, "comres_prune_zombie_alloc",
               actor=actor, detail=detail)
        applied.append({"action": "release_allocation", "node_id": b.node_id,
                        "com": b.com_name, "com_key": b.com_key})

    # 4) reject/revoked 申请若仍挂 binding → 清（兜底）
    bad_applies = (
        session.query(WireLogClientApply)
        .filter(WireLogClientApply.status.in_(("rejected", "revoked")))
        .filter(WireLogClientApply.issued_node_id.isnot(None))
        .all()
    )
    for row in bad_applies:
        nid = row.issued_node_id
        cnt = (session.query(WireLogPortBinding)
               .filter_by(node_id=nid).count())
        if cnt:
            removed = clear_node_bindings(
                session, nid, actor=actor,
                reason=f"apply_{row.status}")
            applied.append({
                "action": f"clear_bindings_{row.status}",
                "node_id": nid, "bindings_removed": removed,
                "request_id": row.id,
            })

    session.commit()
    return {
        "dry_run": False,
        "ok": True,
        **plan,
        "applied": applied,
        "applied_count": len(applied),
    }
