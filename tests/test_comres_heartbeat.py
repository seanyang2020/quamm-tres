"""COMRes 心跳间隔：节点上报优先、按节点 stale、list_nodes 只读覆盖。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain import comres, comres_prune, wirelog
from app.domain.catalog import DomainError
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


def test_register_persists_reported_interval_60(session):
    p = _prov(session, "hb60")
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.1.1",
        "heartbeat_interval_s": 60,
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.1.1:9101",
                   "health_state": "online"}],
    })
    assert r["heartbeat_interval_s"] == 60
    node = session.get(WireLogNode, p["node_id"])
    assert (node.config_json or {}).get("heartbeat_interval_s") == 60


def test_heartbeat_config_nested_interval(session):
    p = _prov(session, "hb-cfg")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.1.2",
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.1.2:9102",
                   "health_state": "online"}],
    })
    hb = comres.heartbeat(session, p["node_token"], {
        "node_id": p["node_id"],
        "config": {"heartbeat_interval_s": 60},
    })
    assert hb["heartbeat_interval_s"] == 60
    node = session.get(WireLogNode, p["node_id"])
    assert (node.config_json or {}).get("heartbeat_interval_s") == 60


def test_register_default_interval_3600(session):
    p = _prov(session, "hb-default")
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.1.3",
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.1.3:9103",
                   "health_state": "online"}],
    })
    assert r["heartbeat_interval_s"] == 3600
    node = session.get(WireLogNode, p["node_id"])
    assert (node.config_json or {}).get("heartbeat_interval_s") == 3600


def test_bad_heartbeat_interval_rejected(session):
    p = _prov(session, "hb-bad")
    for bad in (0, 99999, -1, "nope"):
        with pytest.raises(DomainError) as ei:
            comres.register(session, p["node_token"], {
                "node_id": p["node_id"], "local_ip": "10.9.1.4",
                "heartbeat_interval_s": bad,
                "ports": [{"com": "COM1", "endpoint": "tcp://10.9.1.4:9104",
                           "health_state": "online"}],
            })
        assert ei.value.code == "COMRES_BAD_HEARTBEAT_INTERVAL"


def test_prune_60s_node_offline_after_4min(session):
    p = _prov(session, "prune60")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.2.1",
        "heartbeat_interval_s": 60,
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.2.1:9201",
                   "health_state": "online"}],
    })
    node = session.get(WireLogNode, p["node_id"])
    assert comres_prune.node_stale_timeout_s(node) == 180
    node.last_heartbeat_at = utcnow() - timedelta(seconds=240)
    session.commit()

    r = comres_prune.prune_maintenance(session, dry_run=False, actor="test")
    assert any(a["action"] == "mark_offline_stale" and a["node_id"] == p["node_id"]
               for a in r["applied"])
    node = session.get(WireLogNode, p["node_id"])
    assert node.status == "offline"
    b = session.query(WireLogPortBinding).filter_by(node_id=p["node_id"]).one()
    assert (b.health_state or "").lower() == "stale"


def test_prune_3600s_node_not_offline_after_4min(session):
    p = _prov(session, "prune3600")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.2.2",
        "heartbeat_interval_s": 3600,
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.2.2:9202",
                   "health_state": "online"}],
    })
    node = session.get(WireLogNode, p["node_id"])
    assert comres_prune.node_stale_timeout_s(node) == 10800
    node.last_heartbeat_at = utcnow() - timedelta(seconds=240)
    session.commit()

    r = comres_prune.prune_maintenance(session, dry_run=False, actor="test")
    assert not any(a["action"] == "mark_offline_stale" and a["node_id"] == p["node_id"]
                   for a in r["applied"])
    node = session.get(WireLogNode, p["node_id"])
    assert node.status == "online"


def test_expire_allocations_per_node_interval(session):
    p60 = _prov(session, "exp60")
    comres.register(session, p60["node_token"], {
        "node_id": p60["node_id"], "local_ip": "10.9.3.1",
        "heartbeat_interval_s": 60,
        "ports": [{"com": "COM60", "sn": "SN60",
                   "endpoint": "tcp://10.9.3.1:9301", "health_state": "online"}],
    })
    p36 = _prov(session, "exp3600")
    comres.register(session, p36["node_token"], {
        "node_id": p36["node_id"], "local_ip": "10.9.3.2",
        "heartbeat_interval_s": 3600,
        "ports": [{"com": "COM36", "sn": "SN36",
                   "endpoint": "tcp://10.9.3.2:9302", "health_state": "online"}],
    })

    for nid in (p60["node_id"], p36["node_id"]):
        node = session.get(WireLogNode, nid)
        node.last_heartbeat_at = utcnow() - timedelta(seconds=200)
        b = session.query(WireLogPortBinding).filter_by(node_id=nid).one()
        assert b.allocated_by and str(b.allocated_by).startswith("auto:")
        b.allocated_expire = utcnow() + timedelta(seconds=3600)
    session.commit()

    released = comres.expire_allocations(session)
    ids = {r["node_id"] for r in released}
    assert p60["node_id"] in ids
    assert p36["node_id"] not in ids

    b60 = session.query(WireLogPortBinding).filter_by(node_id=p60["node_id"]).one()
    b36 = session.query(WireLogPortBinding).filter_by(node_id=p36["node_id"]).one()
    assert b60.allocated_by is None
    assert b36.allocated_by is not None and str(b36.allocated_by).startswith("auto:")


def test_list_nodes_stale_status_and_heartbeat_age(session):
    p = _prov(session, "list-stale")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.4.1",
        "heartbeat_interval_s": 60,
        "ports": [{"com": "COM1", "endpoint": "tcp://10.9.4.1:9401",
                   "health_state": "online"}],
    })
    node = session.get(WireLogNode, p["node_id"])
    node.last_heartbeat_at = utcnow() - timedelta(seconds=250)
    session.commit()
    # DB 仍为 online；列表只读覆盖
    assert node.status == "online"

    rows = wirelog.list_nodes(session)
    hit = next(x for x in rows if x["node_id"] == p["node_id"])
    assert hit["status"] == "stale"
    assert hit["heartbeat_interval_s"] == 60
    assert hit["heartbeat_timeout_s"] == 180
    assert hit["heartbeat_age_s"] is not None and hit["heartbeat_age_s"] >= 250

    node2 = session.get(WireLogNode, p["node_id"])
    assert node2.status == "online"  # 未因读取写库
