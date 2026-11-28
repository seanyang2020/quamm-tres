"""COMRes：排除 retired 匹配后，复用/复活 retired 避免 uq_device_mac/sn 撞库 500。"""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.constants import DeviceStatus
from app.domain import comres, wirelog
from app.migrations import migrate
from app.models import Device, WireLogPortBinding
from app.utils import new_device_id, utcnow


MAC = "fe:93:1a:fd:80:e0"
MAC_ALT = "FE-93-1A-FD-80-E0"  # 同键不同写法


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _prov(session, name="reuse-n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _seed_device(session, *, sn=None, mac=None, ip=None, status="available",
                 name=None) -> Device:
    now = utcnow()
    d = Device(
        device_id=new_device_id(),
        display_name=name or f"dev-{sn or mac or ip}",
        sn=sn,
        mac=mac,
        ip=ip,
        owner="t",
        status=status,
        health_level="L0",
        source="serial",
        register_channel="manual",
        created_at=now,
        last_seen_at=now,
        retired_at=now if status == DeviceStatus.RETIRED else None,
    )
    session.add(d)
    session.commit()
    return d


def _fp(mac="", sn="", ip=""):
    return {
        "schema": "fp.v1", "verdict": "ok",
        "identity": {"mac": mac, "sn": sn, "ip": ip},
        "net": [], "system": {}, "raw": {}, "errors": [],
    }


def _hb(session, p, *, com="COM4", mac="", sn="", ip="", local_ip="172.16.1.10"):
    return comres.heartbeat(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": local_ip,
        "ports": [{
            "com": com, "endpoint": f"tcp://{local_ip}:9204",
            "health_state": "online",
            "fingerprint": _fp(mac=mac, sn=sn, ip=ip),
        }],
    })


def test_retired_mac_reused_on_heartbeat_no_integrity_error(session):
    """库里 retired 占着 mac → 心跳同 mac：复用、status→pending、绑定成功、不 500。"""
    retired = _seed_device(
        session, mac=MAC, ip="10.0.0.9",
        status=DeviceStatus.RETIRED, name="comres-fe:93:1a:fd:80:e0",
    )
    owner_before = retired.owner
    name_before = retired.display_name
    channel_before = retired.register_channel
    source_before = retired.source
    n_before = session.query(Device).count()

    p = _prov(session, "reuse-mac")
    # 先登记空端口，再心跳带指纹（贴近生产：已有 binding 后周期性心跳）
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM4", "endpoint": "tcp://172.16.1.10:9204",
            "health_state": "online",
        }],
    })

    try:
        r = _hb(session, p, mac=MAC, ip="10.0.0.9")
    except IntegrityError:
        pytest.fail("复用 retired 时不应抛 IntegrityError / 心跳 500")

    assert r["ports"][0]["bound_device_id"] == retired.device_id
    session.expire_all()
    d = session.get(Device, retired.device_id)
    assert d.status == DeviceStatus.PENDING
    assert d.retired_at is None
    assert d.display_name == name_before
    assert d.owner == owner_before
    assert d.register_channel == channel_before
    assert d.source == source_before
    assert session.query(Device).count() == n_before
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM4").one())
    assert b.bound_device_id == retired.device_id


def test_retired_mac_heartbeat_idempotent_twice(session):
    """同一心跳连续两次（模拟 60s 轮询）→ 第二次仍成功且设备数不增加。"""
    retired = _seed_device(
        session, mac=MAC, status=DeviceStatus.RETIRED, name="idem-mac",
    )
    p = _prov(session, "reuse-idem")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM4", "endpoint": "tcp://172.16.1.10:9204",
            "health_state": "online",
        }],
    })
    n0 = session.query(Device).count()
    r1 = _hb(session, p, mac=MAC)
    assert r1["ports"][0]["bound_device_id"] == retired.device_id
    n1 = session.query(Device).count()
    r2 = _hb(session, p, mac=MAC_ALT)  # 归一化同键
    assert r2["ports"][0]["bound_device_id"] == retired.device_id
    assert session.query(Device).count() == n1 == n0
    session.expire_all()
    assert session.get(Device, retired.device_id).status == DeviceStatus.PENDING


def test_retired_sn_reused(session):
    """retired 同 SN → 复用复活。"""
    sn = "SN-RETIRED-0001"
    retired = _seed_device(
        session, sn=sn, mac=None, status=DeviceStatus.RETIRED, name="retired-sn",
    )
    p = _prov(session, "reuse-sn")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM5", "endpoint": "tcp://172.16.1.10:9205",
            "health_state": "online",
        }],
    })
    r = comres.heartbeat(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM5", "endpoint": "tcp://172.16.1.10:9205",
            "health_state": "online",
            "fingerprint": _fp(sn=sn),
        }],
    })
    assert r["ports"][0]["bound_device_id"] == retired.device_id
    session.expire_all()
    assert session.get(Device, retired.device_id).status == DeviceStatus.PENDING
    assert session.get(Device, retired.device_id).retired_at is None


def test_non_retired_same_mac_preferred_over_retired(session):
    """存在非 retired 同 mac → 优先绑它，不复活 retired。"""
    retired = _seed_device(
        session, mac=MAC, status=DeviceStatus.RETIRED, name="twin-retired",
    )
    # 唯一索引在原始字符串上：无冒号写法可与冒号 retired 并存（双胞胎）
    available = _seed_device(
        session, mac="fe931afd80e0", status="available", name="twin-live",
    )
    p = _prov(session, "reuse-pref")
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM4", "endpoint": "tcp://172.16.1.10:9204",
            "health_state": "online",
            "fingerprint": _fp(mac=MAC),
        }],
    })
    assert r["ports"][0]["bound_device_id"] == available.device_id
    session.expire_all()
    assert session.get(Device, retired.device_id).status == DeviceStatus.RETIRED
    assert session.get(Device, retired.device_id).retired_at is not None


def test_ip_only_does_not_revive_retired(session):
    """只有 IP 相同、无 mac/sn → 不复活 retired（IP 回退已收紧）。"""
    retired = _seed_device(
        session, mac=None, sn=None, ip="10.9.8.7",
        status=DeviceStatus.RETIRED, name="ip-only-retired",
    )
    p = _prov(session, "reuse-ip")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM4", "endpoint": "tcp://172.16.1.10:9204",
            "health_state": "online",
        }],
    })
    n_before = session.query(Device).count()
    r = _hb(session, p, ip="10.9.8.7")
    # 无身份：miss，不绑定、不新建、不复活
    assert r["ports"][0].get("bound_device_id") in (None, "")
    session.expire_all()
    d = session.get(Device, retired.device_id)
    assert d.status == DeviceStatus.RETIRED
    assert d.retired_at is not None
    assert session.query(Device).count() == n_before


def test_retired_sn_collision_reuses_not_500(session):
    """retired 占着某 sn，新建想写同 sn → 复用而非 IntegrityError。"""
    sn = "SN-COLLIDE-42"
    retired = _seed_device(
        session, sn=sn, mac=None, status=DeviceStatus.RETIRED, name="sn-hold",
    )
    # 直接走建档兜底路径
    port = {"fingerprint": {"identity": {"sn": sn, "mac": "", "ip": ""}}}
    try:
        device = comres._create_device_from_port(session, port, actor="t")
    except IntegrityError:
        pytest.fail("同 sn retired 占用时建档应复用，不应 IntegrityError")
    session.commit()
    assert device.device_id == retired.device_id
    assert device.status == DeviceStatus.PENDING
    assert device.retired_at is None
    assert session.query(Device).filter(Device.sn == sn).count() == 1
