"""COMRes 无效资源清理：stale / dormant / enrolled→retired / revoke 清 binding。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain import comres, comres_apply, comres_prune, wirelog
from app.migrations import migrate
from app.models import WireLogNode, WireLogPortBinding
from app.utils import utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _prov(session, name="n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _register_one(session, prov, *, ip="10.8.1.1", com="COM1", port=9101):
    return comres.register(session, prov["node_token"], {
        "node_id": prov["node_id"], "local_ip": ip,
        "ports": [{"com": com, "endpoint": f"tcp://{ip}:{port}",
                   "health_state": "online"}],
    })


def test_heartbeat_timeout_marks_stale(session):
    p = _prov(session, "stale-node")
    _register_one(session, p)
    node = session.get(WireLogNode, p["node_id"])
    assert node.status == "online"
    # 超过 stale 窗口
    node.last_heartbeat_at = utcnow() - timedelta(
        seconds=comres_prune.stale_timeout_s() + 60)
    session.commit()

    r = comres_prune.prune_maintenance(session, dry_run=False, actor="test")
    actions = {a["action"] for a in r["applied"]}
    assert "mark_offline_stale" in actions

    node = session.get(WireLogNode, p["node_id"])
    assert node.status == "offline"
    b = session.query(WireLogPortBinding).filter_by(node_id=p["node_id"]).one()
    assert (b.health_state or "").lower() == "stale"

    # 资源池：进 inactive，不进 pool
    view = comres.list_resources(session)
    assert not any(x["node_id"] == p["node_id"] for x in view["pool"])
    inactive = [x for x in view["inactive"] if x["node_id"] == p["node_id"]]
    assert inactive and inactive[0]["state"] == "stale"


def test_offline_24h_purges_bindings_and_dormant(session):
    p = _prov(session, "dormant-node")
    _register_one(session, p, port=9102)
    node = session.get(WireLogNode, p["node_id"])
    node.status = "offline"
    node.last_heartbeat_at = utcnow() - timedelta(
        seconds=comres_prune.offline_dormant_s() + 60)
    session.commit()

    r = comres_prune.prune_maintenance(session, dry_run=False, actor="test")
    assert any(a["action"] == "dormant_purge_bindings"
               and a["node_id"] == p["node_id"] for a in r["applied"])

    node = session.get(WireLogNode, p["node_id"])
    assert node.status == "dormant"
    assert (session.query(WireLogPortBinding)
            .filter_by(node_id=p["node_id"]).count()) == 0


def test_enrolled_2h_never_heartbeat_retired(session):
    p = _prov(session, "never-hb")
    # 手工挂一条 binding（enrolled 通常无 binding，测清理路径）
    session.add(WireLogPortBinding(
        node_id=p["node_id"], slot=1, com_name="COM9",
        endpoint="tcp://10.8.9.1:9199", tcp_port=9199,
        com_key="10.8.9.1:9199", health_state="online", baud=115200,
        last_checked_at=utcnow(),
    ))
    node = session.get(WireLogNode, p["node_id"])
    assert node.status == "enrolled"
    assert node.last_heartbeat_at is None
    old = utcnow() - timedelta(seconds=comres_prune.enrolled_retire_s() + 60)
    node.provisioned_at = old
    node.created_at = old
    session.commit()

    r = comres_prune.prune_maintenance(session, dry_run=False, actor="test")
    assert any(a["action"] == "retire_never_heartbeat"
               and a["node_id"] == p["node_id"] for a in r["applied"])

    node = session.get(WireLogNode, p["node_id"])
    assert node.status == "retired"
    assert (session.query(WireLogPortBinding)
            .filter_by(node_id=p["node_id"]).count()) == 0


def test_revoke_clears_bindings(session):
    # 走 apply → register → revoke
    body = comres_apply.apply_client(session, {
        "client_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "display_name": "revoke-desk",
        "model_class": "pc",
        "client_version": "1.0",
    }, source_ip="127.0.0.1")
    assert body["status"] == "approved"
    token, node_id, rid = body["node_token"], body["node_id"], body["request_id"]
    comres.register(session, token, {
        "node_id": node_id, "local_ip": "10.8.3.1",
        "ports": [{"com": "COM3", "endpoint": "tcp://10.8.3.1:9303",
                   "health_state": "online"}],
    })
    assert (session.query(WireLogPortBinding)
            .filter_by(node_id=node_id).count()) == 1

    out = comres_apply.revoke_application(session, rid, actor="admin")
    assert out["status"] == "revoked"
    assert out["bindings_removed"] >= 1
    assert (session.query(WireLogPortBinding)
            .filter_by(node_id=node_id).count()) == 0
    node = session.get(WireLogNode, node_id)
    assert node.status == "retired"


def test_stale_then_release_zombie_alloc_same_pass(session):
    """online 心跳超时同轮转 offline 后，到期 allocated 一并回收。"""
    p = _prov(session, "zombie-alloc")
    _register_one(session, p, port=9122)
    b = session.query(WireLogPortBinding).filter_by(node_id=p["node_id"]).one()
    b.allocated_by = "ghost-holder"
    b.allocated_at = utcnow() - timedelta(hours=2)
    b.allocated_expire = utcnow() - timedelta(minutes=5)
    node = session.get(WireLogNode, p["node_id"])
    node.last_heartbeat_at = utcnow() - timedelta(
        seconds=comres_prune.stale_timeout_s() + 60)
    session.commit()

    r = comres_prune.prune_maintenance(session, dry_run=False, actor="test")
    actions = {a["action"] for a in r["applied"]}
    assert "mark_offline_stale" in actions
    assert "release_allocation" in actions
    b2 = session.query(WireLogPortBinding).filter_by(node_id=p["node_id"]).one()
    assert b2.allocated_by is None
    assert b2.allocated_expire is None


def test_audit_dry_run_shape(session):
    p = _prov(session, "audit-shape")
    _register_one(session, p, port=9111)
    node = session.get(WireLogNode, p["node_id"])
    node.last_heartbeat_at = utcnow() - timedelta(
        seconds=comres_prune.stale_timeout_s() + 10)
    session.commit()

    audit = comres_prune.audit_maintenance(session)
    assert set(audit.keys()) >= {
        "duplicates", "stale", "orphan_nodes", "allocated_zombie", "counts",
    }
    assert any(x["node_id"] == p["node_id"] for x in audit["stale"])
    # dry_run prune 不改库
    r = comres_prune.prune_maintenance(session, dry_run=True)
    assert r["dry_run"] is True
    assert r["applied"] == []
    node2 = session.get(WireLogNode, p["node_id"])
    assert node2.status == "online"
