"""F6：auto:* 绑定算 bound 非 allocated；真实租用仍算 allocated。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain import comres, wirelog
from app.migrations import migrate
from app.models import Device, WireLogNode, WireLogPortBinding
from app.utils import utcnow, new_device_id


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _prov(session, name="n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _seed_device(session, *, sn: str) -> Device:
    now = utcnow()
    d = Device(
        device_id=new_device_id(),
        display_name=f"dev-{sn}",
        sn=sn,
        owner="t",
        status="available",
        health_level="L0",
        source="serial",
        register_channel="manual",
        created_at=now,
        last_seen_at=now,
    )
    session.add(d)
    session.commit()
    return d


def test_auto_sn_is_bound_not_allocated(session):
    p = _prov(session, "auto-desk")
    dev = _seed_device(session, sn="SN-AUTO-001")
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.7.1.1",
        "ports": [{
            "com": "COM16", "endpoint": "tcp://10.7.1.1:9210",
            "health_state": "online", "sn": "SN-AUTO-001",
        }],
    })
    port = r["ports"][0]
    assert port.get("reason") is None
    assert port["allocated"] is False
    assert port["bound"] is True
    assert port["bound_device_id"] == dev.device_id
    assert (port.get("allocated_by") or "").startswith("auto:")

    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM16").one())
    assert comres._is_allocated(b) is False
    assert comres._is_bound(b) is True
    assert comres._allocation_marker_active(b) is True

    view = comres.list_resources(session)
    assert not any(x["com"] == "COM16" and x["node_id"] == p["node_id"]
                   for x in view["allocated"])
    bound_hit = [x for x in view["bound"]
                 if x["node_id"] == p["node_id"] and x["com"] == "COM16"]
    assert len(bound_hit) == 1
    assert bound_hit[0]["state"] == "bound"
    assert bound_hit[0]["allocated"] is False
    assert bound_hit[0]["bound"] is True
    assert (bound_hit[0].get("allocated_by") or "").startswith("auto:")

    # 节点详情 ports 同样补 bound
    detail = wirelog.get_node(session, p["node_id"])
    dp = next(x for x in detail["ports"] if x["com_name"] == "COM16")
    assert dp["allocated"] is False
    assert dp["bound"] is True
    assert (dp.get("allocated_by") or "").startswith("auto:")


def test_real_lease_and_auto_bound_coexist_in_resources(session):
    auto_n = _prov(session, "auto-n")
    lease_n = _prov(session, "lease-n")
    _seed_device(session, sn="SN-COEXIST")

    comres.register(session, auto_n["node_token"], {
        "node_id": auto_n["node_id"], "local_ip": "10.7.2.1",
        "ports": [{
            "com": "COM1", "endpoint": "tcp://10.7.2.1:9301",
            "health_state": "online", "sn": "SN-COEXIST",
        }],
    })
    comres.register(session, lease_n["node_token"], {
        "node_id": lease_n["node_id"], "local_ip": "10.7.2.2",
        "ports": [{
            "com": "COM2", "endpoint": "tcp://10.7.2.2:9302",
            "health_state": "online",
        }],
    })
    lease_b = (session.query(WireLogPortBinding)
               .filter_by(node_id=lease_n["node_id"], com_name="COM2").one())
    comres._mark_allocated(lease_b, by="agent-lease-1", ttl_s=900)
    session.commit()

    view = comres.list_resources(session)
    alloc = [x for x in view["allocated"] if x["node_id"] == lease_n["node_id"]]
    bound = [x for x in view["bound"] if x["node_id"] == auto_n["node_id"]]
    assert len(alloc) == 1
    assert alloc[0]["allocated"] is True
    assert alloc[0]["bound"] is True
    assert alloc[0]["allocated_by"] == "agent-lease-1"
    assert alloc[0]["state"] == "allocated"
    assert len(bound) == 1
    assert bound[0]["allocated"] is False
    assert bound[0]["bound"] is True
    assert bound[0]["state"] == "bound"
    assert (bound[0].get("allocated_by") or "").startswith("auto:")


def test_auto_ttl_expiry_still_clears_marker(session):
    """expire_allocations 对 auto 仍按 TTL 回收（展示口径变了，回收语义不变）。"""
    p = _prov(session, "auto-ttl")
    _seed_device(session, sn="SN-TTL")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.7.3.1",
        "ports": [{
            "com": "COM3", "endpoint": "tcp://10.7.3.1:9303",
            "health_state": "online", "sn": "SN-TTL",
        }],
    })
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM3").one())
    assert (b.allocated_by or "").startswith("auto:")
    b.allocated_expire = utcnow() - timedelta(seconds=1)
    node = session.get(WireLogNode, p["node_id"])
    node.status = "offline"
    session.commit()

    comres.expire_allocations(session)
    session.refresh(b)
    assert b.allocated_by is None
    assert b.bound_device_id is not None  # 绑定关系保留
