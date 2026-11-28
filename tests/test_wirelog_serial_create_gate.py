"""串口建机门禁（wirelog.create_device_from_binding）：

- 自动探测（COMRes 上报 verdict=ok）指纹可建机，不再硬卡 manual_probe/L3；
- identity 无 mac/sn/ip 仍拒；
- verdict=partial 或 net 段所有网卡 DOWN / MAC 全 00:00:00:00:00:00 拒绝。
"""
from __future__ import annotations

import pytest

from app.domain import comres, wirelog
from app.domain.catalog import DomainError
from app.migrations import migrate
from app.models import WireLogPortBinding


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _prov(session, name="gate-n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _register_port(session, p, *, probe, com="COM-GATE"):
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.10.0.1",
        "ports": [{
            "com": com, "endpoint": "tcp://10.10.0.1:9451",
            "health_state": "online",
            "probe": probe,
        }],
    })
    return (session.query(WireLogPortBinding)
            .filter_by(node_id=p["node_id"], com_name=com).one())


def _probe(*, verdict="ok", mac="aa:bb:cc:dd:ee:01", sn=None, ip=None, net=None):
    return {
        "verdict": verdict,
        "identity": {"mac": mac or "", "sn": sn or "", "ip": ip or ""},
        "net": net if net is not None else
               [{"iface": "eth0", "mac": mac or "00:00:00:00:00:00",
                 "ip": ip or "10.10.0.9", "up": True}],
        "system": {"project": "", "model": ""},
    }


def test_auto_ok_fingerprint_can_create_device(session):
    """自动探测 ok 指纹（identity 有 mac）可建机，不再要求 manual_probe/L3。"""
    p = _prov(session, "gate-ok")
    b = _register_port(session, p, probe=_probe())
    r = wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert r["result"] in ("created", "existing")
    assert r["device_id"]


def test_no_identity_still_rejected(session):
    """无 mac/sn/ip 仍报 SERIAL_NOT_L3，不放宽到无身份可建机。"""
    p = _prov(session, "gate-noid")
    b = _register_port(session, p, probe=_probe(mac=None))
    with pytest.raises(DomainError) as ei:
        wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert ei.value.code == "SERIAL_NOT_L3"


def test_net_all_down_rejected(session):
    """net 段所有网卡 DOWN → 残留假指纹，按 L2 拒绝。"""
    p = _prov(session, "gate-down")
    b = _register_port(session, p, probe=_probe(
        net=[{"iface": "eth0", "mac": "aa:bb:cc:dd:ee:01", "ip": "10.0.0.2", "up": False}]))
    with pytest.raises(DomainError) as ei:
        wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert ei.value.code == "SERIAL_NOT_L3"


def test_net_all_zero_mac_rejected(session):
    """net 段 MAC 全 00:00:00:00:00:00 → 拒绝（COM4 场景）。"""
    p = _prov(session, "gate-zero")
    b = _register_port(session, p, probe=_probe(
        net=[{"iface": "eth0", "mac": "00:00:00:00:00:00", "ip": "", "up": True}]))
    with pytest.raises(DomainError) as ei:
        wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert ei.value.code == "SERIAL_NOT_L3"


def test_partial_verdict_rejected(session):
    """verdict=partial 半截指纹 → 拒绝。"""
    p = _prov(session, "gate-partial")
    b = _register_port(session, p, probe=_probe(verdict="partial"))
    with pytest.raises(DomainError) as ei:
        wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert ei.value.code == "SERIAL_NOT_L3"


def test_health_online_without_shell_reachable_can_create(session):
    """shell_reachable=False 但 health_state=online → 仍可建机（reachable 口径）。"""
    p = _prov(session, "gate-hs-online")
    b = _register_port(session, p, probe=_probe(), com="COM-HS-ON")
    b.shell_reachable = False
    b.health_state = "online"
    session.commit()
    r = wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert r["result"] in ("created", "existing")
    assert r["device_id"]


def test_open_failed_with_mac_rejected(session):
    """health_state=open_failed 且指纹有 mac → 按当前实现拒绝（SERIAL_NOT_L3）。

    reachable 要求 shell_reachable 或 health_state∈{online,probed}；
    open_failed 不在其中且默认 shell_reachable=False，故建机被拒。
    """
    p = _prov(session, "gate-open-fail")
    b = _register_port(session, p, probe=_probe(mac="aa:bb:cc:dd:ee:99"),
                       com="COM-OF")
    b.shell_reachable = False
    b.health_state = "open_failed"
    session.commit()
    with pytest.raises(DomainError) as ei:
        wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert ei.value.code == "SERIAL_NOT_L3"


def test_partial_with_mac_still_rejected(session):
    """verdict=partial 但 identity 有 mac → 仍被拒（SERIAL_NOT_L3）。"""
    p = _prov(session, "gate-partial-mac")
    b = _register_port(session, p, probe=_probe(
        verdict="partial", mac="aa:bb:cc:dd:ee:88"), com="COM-PM")
    with pytest.raises(DomainError) as ei:
        wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert ei.value.code == "SERIAL_NOT_L3"


def test_mac_normalized_hits_existing(session):
    """已有设备冒号 MAC ↔ 指纹横线 MAC → existing，不新建。"""
    from app.constants import DeviceStatus
    from app.models import Device
    from app.utils import new_device_id, utcnow

    now = utcnow()
    existing = Device(
        device_id=new_device_id(),
        display_name="dev-mac-norm",
        mac="a0:ac:78:a0:d7:02",
        owner="t",
        status=DeviceStatus.AVAILABLE,
        health_level="L0",
        source="manual",
        register_channel="manual",
        created_at=now,
        last_seen_at=now,
    )
    session.add(existing)
    session.commit()

    p = _prov(session, "gate-mac-norm")
    b = _register_port(session, p, probe=_probe(mac="A0-AC-78-A0-D7-02"),
                       com="COM-MN")
    r = wirelog.create_device_from_binding(session, p["node_id"], b.slot,
                                           owner="t", actor="t")
    assert r["result"] == "existing"
    assert r["device_id"] == existing.device_id
