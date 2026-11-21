"""t966：节点授权口径统一（auth_source）+ 串口默认写权限。"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.api.wirelog_router import _shell_write_access
from app.domain import comres_apply, wirelog
from app.migrations import migrate
from app.models import Credential, Principal, WireLogClientApply, WireLogNode
from app.utils import hash_token, new_token, token_prefix, utcnow
from app.secret_crypto import encrypt_secret
import importlib.util
from pathlib import Path

_BF = Path(__file__).resolve().parent.parent / "scripts" / "backfill_enroll_applies.py"
_spec = importlib.util.spec_from_file_location("backfill_enroll_applies", _BF)
backfill_mod = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(backfill_mod)


@pytest.fixture
def client(db):
    s = db.session()
    try:
        migrate(s)
    finally:
        s.close()
    from app.main import app
    return TestClient(app)


@pytest.fixture
def admin_cookie(db):
    s = db.session()
    try:
        cred = s.query(Credential).filter_by(role="admin").first()
        if cred is None:
            cred = s.query(Credential).first()
            assert cred is not None
            p = s.get(Principal, cred.principal_id)
            p.is_admin = True
            cred.role = "admin"
            s.commit()
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}
    finally:
        s.close()


def _add_token_only_node(session, *, node_id="wl_tok_only_01", name="esl2-tok"):
    plain = new_token()
    now = utcnow()
    node = WireLogNode(
        node_id=node_id,
        display_name=name,
        model_class="esl2_hk",
        node_token_hash=hash_token(plain),
        token_prefix=token_prefix(plain),
        token_cipher=encrypt_secret(plain),
        hmac_key="ab" * 16,
        status="online",
        provisioned_at=now,
        created_at=now,
    )
    session.add(node)
    session.commit()
    return node, plain


def test_token_only_node_auth_source_token(client, db, admin_cookie):
    s = db.session()
    try:
        node, _ = _add_token_only_node(s)
        node_id = node.node_id
    finally:
        s.close()

    r = client.get("/api/wirelog/nodes", cookies=admin_cookie)
    assert r.status_code == 200, r.text
    row = next(x for x in r.json() if x["node_id"] == node_id)
    assert row["auth_status"] == "approved"
    assert row["auth_source"] == "token"

    d = client.get(f"/api/wirelog/nodes/{node_id}", cookies=admin_cookie)
    assert d.status_code == 200
    assert d.json()["auth_status"] == "approved"
    assert d.json()["auth_source"] == "token"


def test_enroll_writes_apply_idempotent(client, db, admin_cookie):
    body = {
        "display_name": "enroll-desk",
        "model_class": "pc",
        "adb_address": "172.16.60.199:5555",
    }
    r1 = client.post("/api/wirelog/enroll", json=body, cookies=admin_cookie)
    assert r1.status_code == 200, r1.text
    node_id = r1.json()["node_id"]

    s = db.session()
    try:
        rows = (
            s.query(WireLogClientApply)
            .filter_by(client_id=f"enroll:{node_id}")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "approved"
        assert rows[0].source == "enroll"
        assert rows[0].issued_node_id == node_id
        assert rows[0].note == "enroll 预置 token"
        first_id = rows[0].id
    finally:
        s.close()

    # 同 adb 复用节点重发：不重复插入 apply
    r2 = client.post("/api/wirelog/enroll", json=body, cookies=admin_cookie)
    assert r2.status_code == 200, r2.text
    assert r2.json()["node_id"] == node_id
    assert r2.json()["reused"] is True

    s = db.session()
    try:
        rows = (
            s.query(WireLogClientApply)
            .filter_by(client_id=f"enroll:{node_id}")
            .all()
        )
        assert len(rows) == 1
        assert rows[0].id == first_id
        node = s.get(WireLogNode, node_id)
        brief = wirelog._node_brief(
            s, node, apply_rows=comres_apply.auth_rows_by_node(s))
        assert brief["auth_status"] == "approved"
        assert brief["auth_source"] == "token"
    finally:
        s.close()


def test_backfill_enroll_applies_idempotent(db):
    s = db.session()
    try:
        migrate(s)
        node, _ = _add_token_only_node(s, node_id="wl_bf_01", name="bf-node")
        node_id = node.node_id
    finally:
        s.close()

    r1 = backfill_mod.run(dry_run=False)
    assert r1["candidates"] >= 1
    assert any(c["node_id"] == node_id for c in r1["created"])
    assert r1["remaining_after"] == 0

    r2 = backfill_mod.run(dry_run=False)
    assert r2["candidates"] == 0
    assert r2["remaining_after"] == 0

    dry = backfill_mod.run(dry_run=True)
    assert dry["candidates"] == 0

    s = db.session()
    try:
        row = s.query(WireLogClientApply).filter_by(
            client_id=f"enroll:{node_id}").one()
        assert row.status == "approved"
        assert row.source == "enroll"
        node = s.get(WireLogNode, node_id)
        st, src = comres_apply.resolve_node_auth(node, row)
        assert (st, src) == ("approved", "token")
    finally:
        s.close()


def test_shell_can_write_by_auth_status(db):
    s = db.session()
    try:
        migrate(s)
        approved, _ = _add_token_only_node(s, node_id="wl_w_ok", name="ok")
        # pending 节点：有 apply pending，无有效授权
        now = utcnow()
        pending = WireLogNode(
            node_id="wl_w_pend",
            display_name="pend",
            model_class="pc",
            node_token_hash=hash_token("x"),
            token_prefix="rr_pending_",
            hmac_key="cd" * 16,
            status="enrolled",
            provisioned_at=now,
            created_at=now,
        )
        s.add(pending)
        s.flush()
        s.add(WireLogClientApply(
            client_id="pend-client-1",
            display_name="pend",
            status="pending",
            request_count=1,
            first_seen_at=now,
            last_seen_at=now,
            issued_node_id=pending.node_id,
            source="apply",
        ))
        s.commit()

        can_ok, mode_ok, _ = _shell_write_access(s, approved)
        assert can_ok is True and mode_ok == "rw"

        can_p, mode_p, reason = _shell_write_access(s, pending)
        assert can_p is False and mode_p == "ro"
        assert "未授权" in reason
    finally:
        s.close()


def test_write_grant_still_200_but_not_gate(client, db, admin_cookie):
    s = db.session()
    try:
        migrate(s)
        node, _ = _add_token_only_node(s, node_id="wl_wg_01", name="wg")
        node_id = node.node_id
        # 需要一条 binding 才能 grant
        from app.models import WireLogPortBinding
        s.add(WireLogPortBinding(
            node_id=node_id, slot=1, baud=115200, health_state="online",
            endpoint="tcp://127.0.0.1:9210", com_name="ttyUSB0",
        ))
        s.commit()
    finally:
        s.close()

    r = client.post(
        f"/api/wirelog/nodes/{node_id}/write-grant",
        json={"slot": 1, "principal": "user@corp.com"},
        cookies=admin_cookie,
    )
    assert r.status_code == 200, r.text

    s = db.session()
    try:
        node = s.get(WireLogNode, node_id)
        can_write, write_mode, _ = _shell_write_access(s, node)
        assert can_write is True
        assert write_mode == "rw"
        # 即使撤销写授权，approved 节点仍可写
        wirelog.revoke_write(s, node_id, 1, actor="admin")
        can2, mode2, _ = _shell_write_access(s, node)
        assert can2 is True and mode2 == "rw"
    finally:
        s.close()


def test_is_node_pool_authorized_unchanged_for_token_only(db):
    """无 apply 的 token 节点：auth_map 无键 → 仍可入池。"""
    s = db.session()
    try:
        migrate(s)
        node, _ = _add_token_only_node(s, node_id="wl_pool_01")
        auth_map = comres_apply.auth_status_by_node(s)
        assert node.node_id not in auth_map
        assert comres_apply.is_node_pool_authorized(node.node_id, auth_map) is True
    finally:
        s.close()


def test_resolve_apply_vs_token_source():
    node = SimpleNamespace(token_prefix="rr_abc_", status="online")
    apply_row = SimpleNamespace(
        status="approved", source="apply", client_id="uuid-1")
    enroll_row = SimpleNamespace(
        status="approved", source="enroll", client_id="enroll:wl1")
    assert comres_apply.resolve_node_auth(node, apply_row) == ("approved", "apply")
    assert comres_apply.resolve_node_auth(node, enroll_row) == ("approved", "token")
    assert comres_apply.resolve_node_auth(node, None) == ("approved", "token")
    bare = SimpleNamespace(token_prefix=None, status="enrolled")
    assert comres_apply.resolve_node_auth(bare, None) == ("none", "none")
