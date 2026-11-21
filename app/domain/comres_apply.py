"""COMRes 客户端自助申请：默认自动批准、人工审批、离线幂等取 token。

token 明文仅出现在 API 响应；库内存 Fernet 密文（token_cipher）+ 节点侧哈希，
同一 client_id 重复申请返回字节一致的同一 token。
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque

from sqlalchemy import case
from sqlalchemy.orm import Session

from app.domain.catalog import DomainError
from app.domain import wirelog
from app.models import Setting, WireLogClientApply, WireLogNode, WireLogPortBinding
from app.secret_crypto import decrypt_secret, encrypt_secret
from app.utils import hash_token, new_id, new_token, token_prefix, utcnow

SETTING_AUTO_APPROVE = "comres_auto_approve"
# 与 COMRes 节点心跳缺省一致（契约 §2.7）
HEARTBEAT_INTERVAL_S = 3600

# 简易内存限流：每 IP 每分钟最多 N 次 apply / 轮询
_RATE_LIMIT = 60
_RATE_WINDOW_S = 60.0
_rate_lock = threading.Lock()
_rate_buckets: dict[str, deque[float]] = defaultdict(deque)


def check_rate_limit(key: str, *, limit: int = _RATE_LIMIT,
                     window_s: float = _RATE_WINDOW_S) -> None:
    """超限抛 DomainError COMRES_RATE_LIMITED。"""
    now = time.monotonic()
    with _rate_lock:
        q = _rate_buckets[key]
        while q and now - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            raise DomainError(
                "COMRES_RATE_LIMITED",
                f"请求过于频繁，请稍后再试（{limit}/{int(window_s)}s）",
            )
        q.append(now)


def get_auto_approve(session: Session) -> bool:
    row = session.get(Setting, SETTING_AUTO_APPROVE)
    if row is None or row.value is None or row.value == "":
        return True  # 默认开启
    return str(row.value).strip().lower() in ("1", "true", "yes", "on")


def set_auto_approve(session: Session, enabled: bool) -> bool:
    stored = "true" if enabled else "false"
    row = session.get(Setting, SETTING_AUTO_APPROVE)
    now = utcnow()
    if row is None:
        session.add(Setting(key=SETTING_AUTO_APPROVE, value=stored, updated_at=now))
    else:
        row.value = stored
        row.updated_at = now
    session.commit()
    return enabled


def _plain_token(row: WireLogClientApply) -> str | None:
    if not row.token_cipher:
        return None
    return decrypt_secret(row.token_cipher)


def _issue_node_and_token(session: Session, row: WireLogClientApply,
                          actor: str | None) -> str:
    """签发或复用节点与 token；返回明文 token。已有 cipher 则不重新生成。"""
    plain = _plain_token(row)
    if plain and row.issued_node_id:
        node = session.get(WireLogNode, row.issued_node_id)
        if node is not None and node.status != "retired":
            if row.display_name:
                node.display_name = row.display_name
            if row.model_class:
                node.model_class = row.model_class
            return plain
        # 节点已退役：清空后重建
        row.issued_node_id = None
        row.token_cipher = None
        plain = None

    now = utcnow()
    if plain is None:
        plain = new_token()

    if row.issued_node_id:
        node = session.get(WireLogNode, row.issued_node_id)
        if node is None or node.status == "retired":
            row.issued_node_id = None
            node = None
        else:
            node.node_token_hash = hash_token(plain)
            node.token_prefix = token_prefix(plain)
            node.token_cipher = encrypt_secret(plain)
            if row.display_name:
                node.display_name = row.display_name
            if row.model_class:
                node.model_class = row.model_class
    else:
        node = None

    if node is None:
        node_id = new_id("wl", 8)
        cipher = encrypt_secret(plain)
        node = WireLogNode(
            node_id=node_id,
            display_name=row.display_name or node_id,
            model_class=row.model_class,
            node_token_hash=hash_token(plain),
            token_prefix=token_prefix(plain),
            token_cipher=cipher,
            hmac_key=secrets.token_hex(16),
            status="enrolled",
            provisioned_at=now,
            created_at=now,
        )
        session.add(node)
        session.flush()
        row.issued_node_id = node_id
        wirelog._audit(
            session, node_id, "comres_apply_issue",
            actor=actor, detail={"client_id": row.client_id},
        )

    cipher = encrypt_secret(plain)
    row.token_cipher = cipher
    row.token_prefix = token_prefix(plain)
    if node is not None:
        node.token_cipher = cipher
        node.token_prefix = token_prefix(plain)
    return plain


def _approve_row(session: Session, row: WireLogClientApply,
                 actor: str | None, note: str | None = None) -> str:
    plain = _issue_node_and_token(session, row, actor)
    now = utcnow()
    row.status = "approved"
    row.approved_at = now
    row.approved_by = actor or row.approved_by or "system"
    row.rejected_at = None
    row.reject_reason = None
    if note is not None:
        row.note = note
    wirelog._audit(
        session, row.issued_node_id, "comres_apply_approve",
        actor=actor, detail={"client_id": row.client_id, "request_id": row.id},
    )
    return plain


def _approved_payload(row: WireLogClientApply, plain: str) -> dict:
    return {
        "status": "approved",
        "request_id": row.id,
        "node_id": row.issued_node_id,
        "node_token": plain,
        "expires_at": None,
        "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
    }


def _touch(row: WireLogClientApply, *, source_ip=None, display_name=None,
           model_class=None, client_version=None, bump_count=True):
    now = utcnow()
    row.last_seen_at = now
    if bump_count:
        row.request_count = int(row.request_count or 0) + 1
    if source_ip:
        row.source_ip = source_ip
    if display_name:
        row.display_name = display_name
    if model_class is not None:
        row.model_class = model_class
    if client_version is not None:
        row.client_version = client_version


def apply_client(session: Session, payload: dict, *, source_ip: str | None = None) -> dict:
    """客户端申请。auto_approve 时直接签发；已批准幂等返回同一 token。"""
    client_id = (payload.get("client_id") or "").strip()
    if not client_id:
        raise DomainError("COMRES_NO_CLIENT_ID", "client_id 必填")
    if len(client_id) > 64:
        raise DomainError("COMRES_BAD_CLIENT_ID", "client_id 过长")

    display_name = (payload.get("display_name") or "").strip() or None
    model_class = (payload.get("model_class") or "").strip() or None
    client_version = (payload.get("client_version") or "").strip() or None
    auto = get_auto_approve(session)
    now = utcnow()

    row = (session.query(WireLogClientApply)
           .filter_by(client_id=client_id).one_or_none())

    if row is not None and row.status == "rejected":
        _touch(row, source_ip=source_ip, display_name=display_name,
               model_class=model_class, client_version=client_version)
        session.commit()
        return {
            "status": "rejected",
            "request_id": row.id,
            "reason": row.reject_reason or "rejected",
        }

    if row is not None and row.status == "approved":
        _touch(row, source_ip=source_ip, display_name=display_name,
               model_class=model_class, client_version=client_version)
        plain = _plain_token(row)
        node = (session.get(WireLogNode, row.issued_node_id)
                if row.issued_node_id else None)
        # 节点仍在且非退役：幂等返回原 token；否则视为已删除/退役，重新签发
        if plain and node is not None and node.status != "retired":
            if display_name:
                node.display_name = display_name
            if model_class:
                node.model_class = model_class
        else:
            row.issued_node_id = None
            row.token_cipher = None
            plain = _approve_row(session, row, actor="system-reissue")
        session.commit()
        return _approved_payload(row, plain)

    if row is not None and row.status == "revoked":
        # 撤销后再次申请：开启新一轮（清 token，可重新签发新节点）
        _touch(row, source_ip=source_ip, display_name=display_name,
               model_class=model_class, client_version=client_version)
        row.issued_node_id = None
        row.token_cipher = None
        row.token_prefix = None
        row.reject_reason = None
        row.rejected_at = None
        if auto:
            plain = _approve_row(session, row, actor="system-auto")
            session.commit()
            return _approved_payload(row, plain)
        row.status = "pending"
        session.commit()
        return {"status": "pending", "request_id": row.id}

    if row is not None and row.status == "pending":
        _touch(row, source_ip=source_ip, display_name=display_name,
               model_class=model_class, client_version=client_version)
        if auto:
            plain = _approve_row(session, row, actor="system-auto")
            session.commit()
            return _approved_payload(row, plain)
        session.commit()
        return {"status": "pending", "request_id": row.id}

    # 新建
    row = WireLogClientApply(
        client_id=client_id,
        display_name=display_name or client_id[:32],
        model_class=model_class,
        client_version=client_version,
        source_ip=source_ip,
        status="pending",
        request_count=1,
        first_seen_at=now,
        last_seen_at=now,
    )
    session.add(row)
    session.flush()
    if auto:
        plain = _approve_row(session, row, actor="system-auto")
        session.commit()
        return _approved_payload(row, plain)
    session.commit()
    return {"status": "pending", "request_id": row.id}


def get_apply_status(session: Session, client_id: str) -> dict:
    """客户端轮询；approved 时幂等回带同一 node_token。"""
    client_id = (client_id or "").strip()
    if not client_id:
        raise DomainError("COMRES_NO_CLIENT_ID", "client_id 必填")
    row = (session.query(WireLogClientApply)
           .filter_by(client_id=client_id).one_or_none())
    if row is None:
        raise DomainError("COMRES_APPLY_NOT_FOUND", "申请不存在")

    row.last_seen_at = utcnow()
    session.commit()

    if row.status == "approved":
        plain = _plain_token(row)
        if not plain:
            raise DomainError("COMRES_TOKEN_MISSING", "已批准但 token 不可用，请重新申请")
        return {
            "status": "approved",
            "request_id": row.id,
            "node_id": row.issued_node_id,
            "node_token": plain,
            "expires_at": None,
            "heartbeat_interval_s": HEARTBEAT_INTERVAL_S,
        }
    if row.status == "rejected":
        return {
            "status": "rejected",
            "request_id": row.id,
            "reason": row.reject_reason or "rejected",
        }
    if row.status == "revoked":
        return {
            "status": "revoked",
            "request_id": row.id,
            "reason": row.reject_reason or "revoked",
        }
    return {"status": "pending", "request_id": row.id}


def _row_public(row: WireLogClientApply) -> dict:
    return {
        "request_id": row.id,
        "client_id": row.client_id,
        "display_name": row.display_name,
        "model_class": row.model_class,
        "client_version": row.client_version,
        "source_ip": row.source_ip,
        "status": row.status,
        "request_count": row.request_count,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "approved_by": row.approved_by,
        "rejected_at": row.rejected_at.isoformat() if row.rejected_at else None,
        "reject_reason": row.reject_reason,
        "issued_node_id": row.issued_node_id,
        "token_prefix": row.token_prefix,
        "note": row.note,
    }


def list_applications(session: Session, *, status: str | None = None,
                      page: int = 1, page_size: int = 50) -> dict:
    page = max(1, int(page or 1))
    page_size = min(200, max(1, int(page_size or 50)))
    q = session.query(WireLogClientApply)
    if status:
        q = q.filter(WireLogClientApply.status == status)
    # pending 优先，其余按 last_seen 倒序
    status_order = case(
        (WireLogClientApply.status == "pending", 0),
        (WireLogClientApply.status == "approved", 1),
        (WireLogClientApply.status == "rejected", 2),
        else_=3,
    )
    q = q.order_by(status_order, WireLogClientApply.last_seen_at.desc())
    total = q.count()
    rows = q.offset((page - 1) * page_size).limit(page_size).all()
    pending_count = (
        session.query(WireLogClientApply)
        .filter_by(status="pending").count()
    )
    last_approved = (
        session.query(WireLogClientApply)
        .filter_by(status="approved")
        .order_by(WireLogClientApply.approved_at.desc())
        .first()
    )
    return {
        "items": [_row_public(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pending_count": pending_count,
        "last_approved_at": (
            last_approved.approved_at.isoformat()
            if last_approved and last_approved.approved_at else None
        ),
        "auto_approve": get_auto_approve(session),
    }


def approve_application(session: Session, request_id: int,
                        actor: str | None = None, note: str | None = None) -> dict:
    row = session.get(WireLogClientApply, int(request_id))
    if row is None:
        raise DomainError("COMRES_APPLY_NOT_FOUND", "申请不存在")
    if row.status == "revoked":
        raise DomainError("COMRES_APPLY_REVOKED", "已撤销，请让客户端重新申请")
    if row.status == "approved" and row.token_cipher and row.issued_node_id:
        plain = _plain_token(row)
        session.commit()
        return _approved_payload(row, plain)
    plain = _approve_row(session, row, actor=actor, note=note)
    session.commit()
    return _approved_payload(row, plain)


def reject_application(session: Session, request_id: int, reason: str,
                       actor: str | None = None) -> dict:
    reason = (reason or "").strip()
    if not reason:
        raise DomainError("COMRES_NO_REASON", "拒绝原因必填")
    row = session.get(WireLogClientApply, int(request_id))
    if row is None:
        raise DomainError("COMRES_APPLY_NOT_FOUND", "申请不存在")
    if row.status == "revoked":
        raise DomainError("COMRES_APPLY_REVOKED", "已撤销的申请不可拒绝")
    now = utcnow()
    row.status = "rejected"
    row.rejected_at = now
    row.reject_reason = reason
    # t942：reject 立即清理 binding（与 revoke 一致）
    removed = 0
    if row.issued_node_id:
        removed = (
            session.query(WireLogPortBinding)
            .filter_by(node_id=row.issued_node_id)
            .delete(synchronize_session=False)
        )
    wirelog._audit(
        session, row.issued_node_id, "comres_apply_reject",
        actor=actor, detail={
            "client_id": row.client_id, "reason": reason,
            "bindings_removed": removed,
        },
    )
    session.commit()
    return {
        "status": "rejected", "request_id": row.id, "reason": reason,
        "bindings_removed": removed,
    }


def revoke_application(session: Session, request_id: int,
                       actor: str | None = None) -> dict:
    row = session.get(WireLogClientApply, int(request_id))
    if row is None:
        raise DomainError("COMRES_APPLY_NOT_FOUND", "申请不存在")
    node_id = row.issued_node_id
    # 资源下架：删除该节点端口绑定
    removed = 0
    if node_id:
        removed = (
            session.query(WireLogPortBinding)
            .filter_by(node_id=node_id)
            .delete(synchronize_session=False)
        )
        node = session.get(WireLogNode, node_id)
        if node is not None and node.status != "retired":
            node.status = "retired"
            wirelog._audit(session, node_id, "retire", actor=actor,
                           detail={"via": "comres_apply_revoke"})

    row.status = "revoked"
    row.reject_reason = row.reject_reason or "revoked"
    # 保留 cipher 无意义（节点已退役）；清空避免误用
    row.token_cipher = None
    wirelog._audit(
        session, node_id, "comres_apply_revoke",
        actor=actor,
        detail={"client_id": row.client_id, "bindings_removed": removed},
    )
    session.commit()
    return {
        "status": "revoked",
        "request_id": row.id,
        "node_id": node_id,
        "bindings_removed": removed,
    }


def auth_status_by_node(session: Session) -> dict[str, str]:
    """node_id → apply status（仅有 issued_node_id 的申请）。"""
    rows = (
        session.query(WireLogClientApply)
        .filter(WireLogClientApply.issued_node_id.isnot(None))
        .all()
    )
    return {r.issued_node_id: r.status for r in rows if r.issued_node_id}


def is_node_pool_authorized(node_id: str | None, auth_map: dict[str, str]) -> bool:
    """可分配池口径：已批准，或无申请记录的存量节点（开放登记兼容）。

    pending / rejected / revoked 不进入可分配池，避免未授权资源被领用。
    """
    if not node_id:
        return False
    st = auth_map.get(node_id)
    if st is None:
        return True  # 存量无申请
    return st == "approved"
