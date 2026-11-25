"""绑定语义：适配器序列号 ≠ 设备身份；身份变强后重绑。"""
from __future__ import annotations

import pytest

from app.domain import comres, wirelog
from app.migrations import migrate
from app.models import Device, WireLogPortBinding
from app.utils import new_device_id, utcnow


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def _prov(session, name="n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _seed(session, *, mac=None, sn=None, ip=None, name="d"):
    now = utcnow()
    d = Device(device_id=new_device_id(), display_name=name, mac=mac, sn=sn, ip=ip,
               owner="t", status="available", health_level="L0", source="serial",
               register_channel="manual", created_at=now, last_seen_at=now)
    session.add(d)
    session.commit()
    return d


def test_adapter_serial_is_not_device_identity(session):
    """板侧上报 usb_serial（适配器SN）时，sn 不算设备身份；按 MAC 绑真设备。"""
    p = _prov(session, "bridge")
    dut = _seed(session, mac="a0:ac:78:a0:d7:50", name="DUT-60.198")
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.20.174",
        "ports": [{
            "com": "ttyUSB0", "slot": 1, "endpoint": "tcp://172.16.20.174:9200",
            "health_state": "listen_ok",
            "mac": "a0:ac:78:a0:d7:50", "shell_ip": "172.16.60.198",
            "sn": "A5069RR4", "usb_serial": "A5069RR4",
        }],
    })
    port = r["ports"][0]
    assert port["bound"] is True
    assert port["bound_device_id"] == dut.device_id
    # 不应造出 comres-A5069RR4 这种拿适配器 SN 当设备 SN 的记录
    assert session.query(Device).filter(Device.sn == "A5069RR4").count() == 0


def test_rebind_when_identity_improves(session):
    """首报只有适配器SN → 弱绑定；再报探到 MAC → 重绑到真设备，占位 retired。"""
    p = _prov(session, "bridge2")
    r1 = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.20.174",
        "ports": [{"com": "ttyUSB0", "slot": 1,
                   "endpoint": "tcp://172.16.20.174:9200",
                   "health_state": "listen_ok", "sn": "ADAPTER-SN"}],
    })
    placeholder = r1["ports"][0]["bound_device_id"]
    assert placeholder

    dut = _seed(session, mac="a0:ac:78:a0:d7:50", name="DUT-60.198")
    r2 = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.20.174",
        "ports": [{"com": "ttyUSB0", "slot": 1,
                   "endpoint": "tcp://172.16.20.174:9200",
                   "health_state": "listen_ok",
                   "mac": "a0:ac:78:a0:d7:50", "shell_ip": "172.16.60.198",
                   "sn": "ADAPTER-SN", "usb_serial": "ADAPTER-SN"}],
    })
    assert r2["ports"][0]["bound_device_id"] == dut.device_id
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="ttyUSB0").one())
    assert b.bound_device_id == dut.device_id
    retired = session.get(Device, placeholder)
    assert retired.status == "retired"



@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


def test_unknown_device_with_only_usb_serial(session):
    p = wirelog.provision_host(session, display_name="pc", operator="t")
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.10.25",
        "ports": [{
            "com": "COM16", "endpoint": "tcp://172.16.10.25:9210",
            "health_state": "online", "sn": "0403:6001:AI8ID21CA",
            "usb_serial": "0403:6001:AI8ID21CA",
        }],
    })
    port = r["ports"][0]
    assert port["bound"] is False
    assert port["bound_device_id"] is None
    assert session.query(Device).filter(Device.sn == "0403:6001:AI8ID21CA").count() == 0
    b = session.query(WireLogPortBinding).filter_by(
        node_id=p["node_id"], com_name="COM16").one()
    assert b.bound_device_id is None


def test_stale_placeholder_cleared_when_usb_serial_declared(session):
    """旧占位（拿适配器SN当设备SN）在明确 usb_serial 且无身份后被清掉。"""
    p = _prov(session, "pc")
    base = {"node_id": p["node_id"], "local_ip": "172.16.10.25"}
    r1 = comres.register(session, p["node_token"], {
        **base, "ports": [{"com": "COM16", "endpoint": "tcp://172.16.10.25:9210",
                           "health_state": "online", "sn": "0403:6001:AI8ID21CA"}]})
    first_id = r1["ports"][0]["bound_device_id"]
    assert first_id
    assert r1["ports"][0]["bound"] is True

    r2 = comres.register(session, p["node_token"], {
        **base, "ports": [{"com": "COM16", "endpoint": "tcp://172.16.10.25:9210",
                           "health_state": "online", "sn": "0403:6001:AI8ID21CA",
                           "usb_serial": "0403:6001:AI8ID21CA"}]})
    port = r2["ports"][0]
    assert port["bound"] is False
    assert port["bound_device_id"] is None
    b = session.query(WireLogPortBinding).filter_by(
        node_id=p["node_id"], com_name="COM16").one()
    assert b.bound_device_id is None
    dev = session.query(Device).filter(Device.device_id == first_id).first()
    assert dev is not None and dev.status == "retired"


def test_capability_revision_changes_after_rebind(session):
    """绑定目标设备变化后，capability_revision 必须变化。"""
    from app.domain import capabilities as cap_svc

    p = _prov(session, "rev-bind")
    dut = _seed(session, mac="a0:ac:78:a0:d7:60", name="DUT-rev")
    r1 = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.20.180",
        "ports": [{
            "com": "ttyUSB0", "slot": 1,
            "endpoint": "tcp://172.16.20.180:9200",
            "health_state": "listen_ok",
            "mac": "a0:ac:78:a0:d7:60", "shell_ip": "172.16.60.200",
        }],
    })
    assert r1["ports"][0]["bound_device_id"] == dut.device_id
    rev1 = cap_svc.build_snapshot(session, dut.device_id)["revision"]

    # 改 endpoint → revision 变
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "172.16.20.180",
        "ports": [{
            "com": "ttyUSB0", "slot": 1,
            "endpoint": "tcp://172.16.20.180:9201",
            "health_state": "listen_ok",
            "mac": "a0:ac:78:a0:d7:60", "shell_ip": "172.16.60.200",
        }],
    })
    rev2 = cap_svc.build_snapshot(session, dut.device_id)["revision"]
    assert rev2 != rev1
