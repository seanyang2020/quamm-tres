"""COMRes 设备指纹 MAC 归一化匹配：冒号/无冒号、retired 双胞胎、强制重绑。"""
from __future__ import annotations

import pytest
from sqlalchemy.orm.exc import MultipleResultsFound

from app.constants import DeviceStatus
from app.domain import comres, wirelog
from app.domain.comres import _mac_match_key, _match_device
from app.migrations import migrate
from app.models import Device, WireLogPortBinding
from app.utils import new_device_id, utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _prov(session, name="mac-n"):
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


def test_mac_match_key_strips_separators():
    assert _mac_match_key("A0:AC:78:A0:D6:C6") == "a0ac78a0d6c6"
    assert _mac_match_key("a0ac78a0d6c6") == "a0ac78a0d6c6"
    assert _mac_match_key("a0-ac-78-a0-d6-c6") == "a0ac78a0d6c6"
    assert _mac_match_key("  a0:ac:78:a0:d6:c6  ") == "a0ac78a0d6c6"
    assert _mac_match_key("") is None
    assert _mac_match_key(None) is None
    assert _mac_match_key("not-a-mac") is None


def test_colon_fp_matches_nocolon_available(session):
    """冒号指纹 ↔ 无冒号 available 设备能正确匹配。"""
    available = _seed_device(
        session, mac="a0ac78a0d6c6", ip="172.16.60.75",
        name="智能相册", status="available",
    )
    p = _prov(session, "colon-fp")
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM4", "endpoint": "tcp://172.16.1.10:9204",
            "health_state": "online",
            "fingerprint": {
                "schema": "fp.v1", "verdict": "ok",
                "identity": {
                    "mac": "a0:ac:78:a0:d6:c6",
                    "sn": "",
                    "ip": "172.16.60.75",
                },
                "net": [], "system": {}, "raw": {}, "errors": [],
            },
        }],
    })
    assert r["ports"][0]["bound_device_id"] == available.device_id
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM4").one())
    assert b.allocated_by == "auto:fp_mac"


def test_twin_mac_binds_available_not_retired(session):
    """双胞胎（归一化 MAC 相同：一个 retired、一个 available）只绑 available。"""
    retired = _seed_device(
        session, mac="a0:ac:78:a0:d6:c6", sn="P010010260928000000F",
        ip="172.16.60.75", status=DeviceStatus.RETIRED, name="framex-retired",
    )
    available = _seed_device(
        session, mac="a0ac78a0d6c6", sn=None, ip="172.16.60.75",
        status="available", name="智能相册",
    )
    port = {
        "fingerprint": {
            "identity": {
                "mac": "a0:ac:78:a0:d6:c6",
                "sn": "P010010260928000000F",
                "ip": "172.16.60.75",
            },
        },
    }
    device, basis = _match_device(session, port, "172.16.1.10")
    assert device is not None
    assert device.device_id == available.device_id
    assert device.device_id != retired.device_id
    assert basis == "fp_mac"


def test_sn_path_skips_retired(session):
    """SN 路径不绑 retired 设备。"""
    retired = _seed_device(
        session, sn="P010010260928000000F", mac="a0:ac:78:a0:d6:c6",
        ip="172.16.60.75", status=DeviceStatus.RETIRED, name="retired-sn",
    )
    available = _seed_device(
        session, sn=None, mac=None, ip="172.16.60.75",
        status="available", name="by-ip",
    )
    # 无 MAC（避免走 MAC），SN 命中退役 → 应落到 IP 的 available
    port = {
        "fingerprint": {
            "identity": {
                "mac": "",
                "sn": "P010010260928000000F",
                "ip": "172.16.60.75",
            },
        },
    }
    device, basis = _match_device(session, port, None)
    assert device is not None
    assert device.device_id == available.device_id
    assert device.device_id != retired.device_id
    assert basis == "fp_ip"


def test_bound_retired_forces_rematch_on_heartbeat(session):
    """已绑 retired 设备，心跳后强制重匹配到 available。"""
    retired = _seed_device(
        session, mac="a0:ac:78:a0:d6:c6", sn="P010010260928000000F",
        ip="172.16.60.75", status=DeviceStatus.RETIRED, name="framex-retired",
    )
    available = _seed_device(
        session, mac="a0ac78a0d6c6", sn=None, ip="172.16.60.75",
        status="available", name="智能相册",
    )
    p = _prov(session, "rebinding")
    # 先注册一个无指纹端口，再手工绑到 retired（模拟历史错误绑定）
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM4", "endpoint": "tcp://172.16.1.10:9204",
            "health_state": "online",
        }],
    })
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM4").one())
    b.bound_device_id = retired.device_id
    b.allocated_by = "auto:fp_mac"
    b.allocated_expire = utcnow()
    # 给足够长的 TTL，否则 marker 过期也会重匹配，测不到 prev_retired 分支
    from datetime import timedelta
    b.allocated_expire = utcnow() + timedelta(hours=2)
    session.commit()

    fp = {
        "schema": "fp.v1", "verdict": "ok",
        "identity": {
            "mac": "a0:ac:78:a0:d6:c6",
            "sn": "P010010260928000000F",
            "ip": "172.16.60.75",
        },
        "net": [], "system": {}, "raw": {}, "errors": [],
    }
    r = comres.heartbeat(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.1.10",
        "ports": [{
            "com": "COM4", "endpoint": "tcp://172.16.1.10:9204",
            "health_state": "online", "fingerprint": fp,
        }],
    })
    assert r["ports"][0]["bound_device_id"] == available.device_id
    session.expire_all()
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM4").one())
    assert b.bound_device_id == available.device_id
    assert b.bound_device_id != retired.device_id


def test_normalized_mac_twins_no_multiple_results(session):
    """归一化后多行（retired+available）匹配不抛 MultipleResultsFound。"""
    _seed_device(
        session, mac="a0:ac:78:a0:d6:c6", sn="P010010260928000000F",
        ip="172.16.60.75", status=DeviceStatus.RETIRED, name="retired-twin",
    )
    available = _seed_device(
        session, mac="a0ac78a0d6c6", sn=None, ip="172.16.60.75",
        status="available", name="available-twin",
    )
    port = {
        "mac": "a0:ac:78:a0:d6:c6",
        "fingerprint": {
            "identity": {"mac": "a0:ac:78:a0:d6:c6", "sn": "", "ip": "172.16.60.75"},
        },
    }
    try:
        device, basis = _match_device(session, port, None)
    except MultipleResultsFound:
        pytest.fail("归一化 MAC 双胞胎匹配不应抛 MultipleResultsFound")
    assert device is not None
    assert device.device_id == available.device_id
    assert basis in ("fp_mac", "mac")


def test_binding_identity_drifted_uses_mac_key(session):
    """冒号 vs 无冒号同一 MAC 不算漂移。"""
    p = _prov(session, "drift-key")
    available = _seed_device(session, mac="a0ac78a0d6c6", name="nocolon")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.0.0.1",
        "ports": [{
            "com": "COM1", "endpoint": "tcp://10.0.0.1:9201",
            "health_state": "online", "mac": "a0ac78a0d6c6",
        }],
    })
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM1").one())
    assert b.bound_device_id == available.device_id
    drifted = comres._binding_identity_drifted(
        session, b, {"mac": "a0:ac:78:a0:d6:c6"},
    )
    assert drifted is False
