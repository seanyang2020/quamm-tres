"""COMRes 去重：同 node 同 com upsert；跨 node 端点独占；迁移清重复。"""
from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

from app.domain import comres, wirelog
from app.migrations import (
    migrate,
    _dedupe_comres_bindings,
    _ensure_comres_node_com_unique_index,
)
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


def test_same_node_same_com_register_keeps_one_row(session):
    p = _prov(session, "dup-com")
    tok, nid = p["node_token"], p["node_id"]
    comres.register(session, tok, {
        "node_id": nid, "local_ip": "10.9.1.1",
        "ports": [{"com": "COM4", "endpoint": "tcp://10.9.1.1:9001",
                   "health_state": "online"}],
    })
    comres.register(session, tok, {
        "node_id": nid, "local_ip": "10.9.1.1",
        "ports": [{"com": "COM4", "endpoint": "tcp://10.9.1.1:9002",
                   "health_state": "online"}],
    })
    rows = (session.query(WireLogPortBinding)
            .filter_by(node_id=nid, com_name="COM4").all())
    assert len(rows) == 1
    assert rows[0].tcp_port == 9002
    assert rows[0].com_key == "10.9.1.1:9002"


def test_endpoint_conflict_when_other_online(session):
    a = _prov(session, "keeper")
    b = _prov(session, "challenger")
    now = utcnow()
    # A 先占端点并保持 online
    comres.register(session, a["node_token"], {
        "node_id": a["node_id"], "local_ip": "10.9.2.1",
        "ports": [{"com": "COM24", "endpoint": "tcp://10.9.2.1:9212",
                   "health_state": "online"}],
    })
    node_a = session.get(WireLogNode, a["node_id"])
    assert node_a.status == "online"
    assert node_a.last_heartbeat_at is not None

    r = comres.register(session, b["node_token"], {
        "node_id": b["node_id"], "local_ip": "10.9.2.2",
        "ports": [{"com": "COM24", "endpoint": "tcp://10.9.2.1:9212",
                   "health_state": "online"}],
    })
    ports = r["ports"]
    assert len(ports) == 1
    assert ports[0].get("reason") == "endpoint_conflict"
    # challenger 不进池
    assert (session.query(WireLogPortBinding)
            .filter_by(node_id=b["node_id"]).count()) == 0
    # keeper 仍在
    keep = (session.query(WireLogPortBinding)
            .filter_by(node_id=a["node_id"], com_name="COM24").one())
    assert (keep.health_state or "").lower() == "online"


def test_endpoint_supersede_when_other_offline(session):
    from app.models import WireLogAudit

    a = _prov(session, "old")
    b = _prov(session, "new")
    comres.register(session, a["node_token"], {
        "node_id": a["node_id"], "local_ip": "10.9.3.1",
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.3.1:9300",
                   "health_state": "online"}],
    })
    node_a = session.get(WireLogNode, a["node_id"])
    node_a.status = "offline"
    node_a.last_heartbeat_at = utcnow() - timedelta(hours=5)
    session.commit()

    r = comres.register(session, b["node_token"], {
        "node_id": b["node_id"], "local_ip": "10.9.3.2",
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.3.1:9300",
                   "health_state": "online"}],
    })
    assert r["ports"][0].get("reason") is None
    assert (r["ports"][0].get("health_state") or "").lower() == "online"
    old = (session.query(WireLogPortBinding)
           .filter_by(node_id=a["node_id"], com_name="COM1").one())
    assert (old.health_state or "").lower() == "superseded"
    new = (session.query(WireLogPortBinding)
           .filter_by(node_id=b["node_id"], com_name="COM1").one())
    assert (new.health_state or "").lower() == "online"
    takeovers = (session.query(WireLogAudit)
                 .filter_by(action="comres_endpoint_takeover").all())
    assert len(takeovers) == 1


def test_endpoint_takeover_when_last_checked_stale(session):
    """F5：占位节点仍 online，但 binding.last_checked 过期 → 允许接管。"""
    from app.models import WireLogAudit
    from app.domain.comres_prune import stale_timeout_s

    a = _prov(session, "stale-holder")
    b = _prov(session, "taker")
    comres.register(session, a["node_token"], {
        "node_id": a["node_id"], "local_ip": "10.9.5.1",
        "ports": [{"com": "COM24", "endpoint": "tcp://10.9.5.1:9210",
                   "health_state": "online"}],
    })
    # 节点保持 online + 心跳新；仅 binding 检查时间过期
    node_a = session.get(WireLogNode, a["node_id"])
    node_a.status = "online"
    node_a.last_heartbeat_at = utcnow()
    old = (session.query(WireLogPortBinding)
           .filter_by(node_id=a["node_id"], com_name="COM24").one())
    old.last_checked_at = utcnow() - timedelta(seconds=stale_timeout_s() + 30)
    session.commit()

    r1 = comres.register(session, b["node_token"], {
        "node_id": b["node_id"], "local_ip": "10.9.5.2",
        "ports": [{"com": "COM24", "endpoint": "tcp://10.9.5.1:9210",
                   "health_state": "online"}],
    })
    assert r1["ports"][0].get("reason") is None
    assert (session.query(WireLogPortBinding)
            .filter_by(node_id=b["node_id"], com_name="COM24").count()) == 1
    old = (session.query(WireLogPortBinding)
           .filter_by(node_id=a["node_id"], com_name="COM24").one())
    assert (old.health_state or "").lower() == "superseded"

    # 幂等：再注册不新增 takeover 审计
    before = (session.query(WireLogAudit)
              .filter_by(action="comres_endpoint_takeover").count())
    comres.register(session, b["node_token"], {
        "node_id": b["node_id"], "local_ip": "10.9.5.2",
        "ports": [{"com": "COM24", "endpoint": "tcp://10.9.5.1:9210",
                   "health_state": "online"}],
    })
    after = (session.query(WireLogAudit)
             .filter_by(action="comres_endpoint_takeover").count())
    assert after == before == 1


def test_endpoint_takeover_when_holder_retired(session):
    """F5：占位节点 retired → 接管。"""
    a = _prov(session, "retired-holder")
    b = _prov(session, "newbie")
    comres.register(session, a["node_token"], {
        "node_id": a["node_id"], "local_ip": "10.9.6.1",
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.6.1:9400",
                   "health_state": "online"}],
    })
    node_a = session.get(WireLogNode, a["node_id"])
    node_a.status = "retired"
    session.commit()

    r = comres.register(session, b["node_token"], {
        "node_id": b["node_id"], "local_ip": "10.9.6.2",
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.6.1:9400",
                   "health_state": "online"}],
    })
    assert r["ports"][0].get("reason") is None
    old = (session.query(WireLogPortBinding)
           .filter_by(node_id=a["node_id"]).one())
    assert (old.health_state or "").lower() == "superseded"


def test_migration_dedupe_on_duplicate_rows(session):
    """在含重复数据的库上跑迁移后无 (node_id,com) 重复、跨 node 端点被 superseded。"""
    a = _prov(session, "m-a")
    b = _prov(session, "m-b")
    now = utcnow()
    # fixture 已建唯一索引；模拟历史脏库：先卸索引再插入重复行
    session.execute(text("DROP INDEX IF EXISTS uq_wl_port_node_com"))
    session.commit()

    # 手工插入同 node 同 com 两行（绕过 upsert）
    session.add(WireLogPortBinding(
        node_id=a["node_id"], slot=1, com_name="COM4",
        endpoint="tcp://10.9.4.1:9401", tcp_port=9401,
        com_key="10.9.4.1:9401", health_state="online",
        last_checked_at=now - timedelta(hours=2), baud=115200,
    ))
    session.add(WireLogPortBinding(
        node_id=a["node_id"], slot=2, com_name="COM4",
        endpoint="tcp://10.9.4.1:9402", tcp_port=9402,
        com_key="10.9.4.1:9402", health_state="online",
        last_checked_at=now, baud=115200,
    ))
    # 跨 node 撞端点
    session.add(WireLogPortBinding(
        node_id=a["node_id"], slot=3, com_name="COM24",
        endpoint="tcp://10.9.4.9:9212", tcp_port=9212,
        com_key="10.9.4.9:9212", health_state="online",
        last_checked_at=now - timedelta(hours=1), baud=115200,
    ))
    session.add(WireLogPortBinding(
        node_id=b["node_id"], slot=1, com_name="COM24",
        endpoint="tcp://10.9.4.9:9212", tcp_port=9212,
        com_key="10.9.4.9:9212", health_state="online",
        last_checked_at=now, baud=115200,
    ))
    node_b = session.get(WireLogNode, b["node_id"])
    node_b.status = "online"
    node_b.last_heartbeat_at = now
    session.commit()

    _dedupe_comres_bindings(session)
    _ensure_comres_node_com_unique_index(session)

    com4 = (session.query(WireLogPortBinding)
            .filter_by(node_id=a["node_id"], com_name="COM4").all())
    assert len(com4) == 1
    assert com4[0].tcp_port == 9402

    ep_rows = (
        session.query(WireLogPortBinding)
        .filter_by(com_key="10.9.4.9:9212").all()
    )
    assert len(ep_rows) == 2
    states = {(r.node_id, (r.health_state or "").lower()) for r in ep_rows}
    # b online 更新 → keeper；a → superseded
    assert (b["node_id"], "online") in states
    assert (a["node_id"], "superseded") in states
