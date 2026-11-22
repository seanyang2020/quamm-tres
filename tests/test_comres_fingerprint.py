"""t981：设备指纹落库 / 匹配顺序 MAC→SN→IP / resources 与单查接口。"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import inspect
from starlette.testclient import TestClient

from app import web_auth
from app.domain import comres, wirelog
from app.migrations import migrate
from app.models import Credential, Device, Principal, WireLogAudit, WireLogPortBinding
from app.utils import utcnow, new_device_id


@pytest.fixture
def session(db):
    s = db.session()
    migrate(s)
    yield s
    s.close()


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


def _prov(session, name="fp-n"):
    return wirelog.provision_host(session, display_name=name, operator="t")


def _seed_device(session, *, sn=None, mac=None, ip=None) -> Device:
    now = utcnow()
    d = Device(
        device_id=new_device_id(),
        display_name=f"dev-{sn or mac or ip}",
        sn=sn,
        mac=mac,
        ip=ip,
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


def _sample_fp(**overrides):
    fp = {
        "schema": "fp.v1",
        "collected_at": "2026-11-22T07:00:00Z",
        "source": "probe",
        "pattern_id": "linux-generic",
        "verdict": "ok",
        "identity": {
            "mac": "aa:bb:cc:dd:ee:01",
            "mac_iface": "wlan0",
            "sn": "SN-FP-001",
            "ip": "10.9.1.10",
            "ip_iface": "wlan0",
        },
        "net": [
            {"iface": "wlan0", "mac": "aa:bb:cc:dd:ee:01", "ip": "10.9.1.10", "up": True},
            {"iface": "eth0", "mac": "aa:bb:cc:dd:ee:02", "ip": "10.9.1.11", "up": False},
        ],
        "system": {
            "os": "Linux", "kernel": "5.15.0", "hostname": "dut-1",
            "project": "demo", "model": "X1",
            "build_info": "build-1", "proc_version": "Linux version 5.15",
            "uname": "Linux dut-1 5.15.0",
        },
        "raw": {
            "uname": "Linux dut-1 5.15.0",
            "cmdline": "console=ttyS0",
            "build_info": "build-1",
            "ifconfig": "wlan0 ...",
        },
        "errors": [],
    }
    fp.update(overrides)
    return fp


def test_migrate_fingerprint_column_idempotent(session):
    migrate(session)
    migrate(session)
    cols = {c["name"] for c in inspect(session.get_bind()).get_columns("wl_port_binding")}
    assert "fingerprint_json" in cols


def test_register_persists_fingerprint_and_backfill(session):
    p = _prov(session, "fp-reg")
    fp = _sample_fp()
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.1",
        "ports": [{
            "com": "COM-FP1", "endpoint": "tcp://10.9.0.1:9401",
            "health_state": "online",
            "fingerprint": fp,
        }],
    })
    assert r["ok"] is True
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-FP1").one())
    stored = json.loads(b.fingerprint_json)
    assert stored["schema"] == "fp.v1"
    assert stored["verdict"] == "ok"
    assert b.shell_mac == "aa:bb:cc:dd:ee:01"
    assert b.shell_ip == "10.9.1.10"

    audits = (session.query(WireLogAudit)
              .filter_by(node_id=p["node_id"], action="comres_fingerprint_update")
              .all())
    assert len(audits) == 1
    detail = json.loads(audits[0].detail)
    assert detail["first"] is True
    assert detail["verdict"] == "ok"


def test_heartbeat_without_fingerprint_keeps_old(session):
    p = _prov(session, "fp-keep")
    fp = _sample_fp(verdict="partial")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.2",
        "ports": [{
            "com": "COM-KEEP", "endpoint": "tcp://10.9.0.2:9402",
            "health_state": "online", "fingerprint": fp,
        }],
    })
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-KEEP").one())
    old = b.fingerprint_json
    assert old

    comres.heartbeat(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.2",
        "ports": [{
            "com": "COM-KEEP", "endpoint": "tcp://10.9.0.2:9402",
            "health_state": "online",
            # 无 fingerprint
        }],
    })
    session.refresh(b)
    assert b.fingerprint_json == old
    assert json.loads(b.fingerprint_json)["verdict"] == "partial"


def test_identity_backfill_only_when_shell_empty(session):
    p = _prov(session, "fp-bf")
    fp = _sample_fp()
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.3",
        "ports": [{
            "com": "COM-BF", "endpoint": "tcp://10.9.0.3:9403",
            "health_state": "online",
            "mac": "11:22:33:44:55:66",
            "ip": "10.9.0.99",
            "fingerprint": fp,
        }],
    })
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-BF").one())
    # 顶层 mac/ip 已写入，identity 不覆盖
    assert b.shell_mac == "11:22:33:44:55:66"
    assert b.shell_ip == "10.9.0.99"


def test_match_mac_beats_sn(session):
    """同端口同时带 MAC(命中 A) 与 SN(命中 B) → 应以 MAC 优先。"""
    p = _prov(session, "fp-ord")
    mac_dev = _seed_device(session, mac="aa:bb:cc:11:22:33", sn="SN-OTHER")
    sn_dev = _seed_device(session, sn="SN-TARGET", mac="ff:ff:ff:00:00:01")
    assert mac_dev.device_id != sn_dev.device_id

    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.4",
        "ports": [{
            "com": "COM-ORD", "endpoint": "tcp://10.9.0.4:9404",
            "health_state": "online",
            "sn": "SN-TARGET",
            "mac": "aa:bb:cc:11:22:33",
        }],
    })
    port = r["ports"][0]
    assert port["bound_device_id"] == mac_dev.device_id
    # allocated_by 含 basis
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-ORD").one())
    assert (b.allocated_by or "").startswith("auto:mac")


def test_match_sn_beats_ip(session):
    p = _prov(session, "fp-snip")
    sn_dev = _seed_device(session, sn="SN-WIN", ip="10.1.1.1")
    ip_dev = _seed_device(session, sn="SN-LOSE", ip="10.9.9.9")

    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.5",
        "ports": [{
            "com": "COM-SNIP", "endpoint": "tcp://10.9.0.5:9405",
            "health_state": "online",
            "sn": "SN-WIN",
            "ip": "10.9.9.9",
        }],
    })
    assert r["ports"][0]["bound_device_id"] == sn_dev.device_id
    assert r["ports"][0]["bound_device_id"] != ip_dev.device_id
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-SNIP").one())
    assert (b.allocated_by or "").startswith("auto:sn")


def test_match_fp_mac_basis(session):
    p = _prov(session, "fp-basis")
    mac_dev = _seed_device(session, mac="aa:bb:cc:dd:ee:01")
    sn_dev = _seed_device(session, sn="SN-TOP-ONLY")

    # 顶层 SN 命中 sn_dev；fingerprint.identity.mac 命中 mac_dev → MAC 优先
    fp = _sample_fp()
    fp["identity"] = {
        **fp["identity"],
        "sn": "SN-UNUSED",  # 避免回填撞 sn_dev 唯一约束
    }
    r = comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.6",
        "ports": [{
            "com": "COM-BASIS", "endpoint": "tcp://10.9.0.6:9406",
            "health_state": "online",
            "sn": "SN-TOP-ONLY",
            "fingerprint": fp,
        }],
    })
    assert r["ports"][0]["bound_device_id"] == mac_dev.device_id
    assert r["ports"][0]["bound_device_id"] != sn_dev.device_id
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-BASIS").one())
    assert b.allocated_by == "auto:fp_mac"


def test_resources_returns_fingerprint_fields(session, client, admin_cookie):
    p = _prov(session, "fp-res")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.7",
        "ports": [{
            "com": "COM-RES", "endpoint": "tcp://10.9.0.7:9407",
            "health_state": "online",
            "fingerprint": _sample_fp(verdict="failed"),
        }],
    })
    view = comres.list_resources(session)
    hit = [x for x in (view["pool"] + view["bound"] + view["allocated"]
                       + view["inactive"])
           if x["node_id"] == p["node_id"] and x["com"] == "COM-RES"]
    assert hit
    assert hit[0]["fp_verdict"] == "failed"
    assert isinstance(hit[0]["fingerprint"], dict)
    assert hit[0]["fingerprint"]["schema"] == "fp.v1"

    # HTTP 单查
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-RES").one())
    resp = client.get(
        f"/api/comres/ports/{p['node_id']}/{b.slot}/fingerprint",
        cookies=admin_cookie,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fp_verdict"] == "failed"
    assert body["fingerprint"]["identity"]["mac"] == "aa:bb:cc:dd:ee:01"


def test_fingerprint_change_audits_cleared_fields(session):
    p = _prov(session, "fp-clr")
    fp1 = _sample_fp()
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.8",
        "ports": [{
            "com": "COM-CLR", "endpoint": "tcp://10.9.0.8:9408",
            "health_state": "online", "fingerprint": fp1,
        }],
    })
    fp2 = _sample_fp(verdict="partial")
    fp2["identity"] = {
        "mac": "aa:bb:cc:dd:ee:01",
        "mac_iface": "wlan0",
        "sn": "",  # SN 变为空
        "ip": "10.9.1.10",
        "ip_iface": "wlan0",
    }
    comres.heartbeat(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.8",
        "ports": [{
            "com": "COM-CLR", "endpoint": "tcp://10.9.0.8:9408",
            "health_state": "online", "fingerprint": fp2,
        }],
    })
    audits = (session.query(WireLogAudit)
              .filter_by(node_id=p["node_id"], action="comres_fingerprint_update")
              .order_by(WireLogAudit.id).all())
    assert len(audits) == 2
    detail = json.loads(audits[1].detail)
    assert detail["first"] is False
    assert "identity.sn" in detail["cleared_fields"]


def test_register_ignores_empty_fingerprint(session):
    """空壳指纹（只有 schema/verdict，无任何身份与系统字段）不得入库，也不得覆盖旧值。"""
    p = _prov(session, "fp-empty")
    fp = _sample_fp()
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.1",
        "ports": [{"com": "COM-EMP", "endpoint": "tcp://10.9.0.1:9411",
                   "health_state": "online", "fingerprint": fp}],
    })
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-EMP").one())
    assert b.fingerprint_json is not None

    shell = {
        "schema": "fp.v1", "verdict": "failed",
        "identity": {"mac": "", "sn": "", "ip": ""},
        "net": [],
        "system": {"os": "", "kernel": "", "hostname": "", "project": "",
                   "model": "", "build_info": "", "uname": "", "proc_version": ""},
        "raw": {}, "errors": [],
    }
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.1",
        "ports": [{"com": "COM-EMP", "endpoint": "tcp://10.9.0.1:9411",
                   "health_state": "online", "fingerprint": shell}],
    })
    session.expire_all()
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-EMP").one())
    stored = json.loads(b.fingerprint_json)
    assert stored["identity"]["mac"] == "aa:bb:cc:dd:ee:01"


def test_register_empty_fingerprint_on_fresh_port_stays_null(session):
    p = _prov(session, "fp-empty2")
    comres.register(session, p["node_token"], {
        "node_id": p["node_id"], "local_ip": "10.9.0.2",
        "ports": [{"com": "COM-EMP2", "endpoint": "tcp://10.9.0.2:9421",
                   "health_state": "online",
                   "fingerprint": {"schema": "fp.v1", "verdict": "failed",
                                   "identity": {"mac": ""}, "net": [],
                                   "system": {}, "raw": {}, "errors": []}}],
    })
    b = (session.query(WireLogPortBinding)
         .filter_by(node_id=p["node_id"], com_name="COM-EMP2").one())
    assert b.fingerprint_json is None

