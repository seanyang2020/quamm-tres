"""Web 人工申请/释放/续租设备租约。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.constants import DeviceStatus, HealthLevel, LeaseState
from app.mcp.server import res_apply
from app.migrations import migrate
from app.models import Credential, Device, Lease, Principal, QueueEntry
from app.utils import utcnow


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


def _cookie(db, email: str) -> dict:
    s = db.session()
    try:
        p = s.query(Principal).filter(Principal.email == email).one()
        cred = s.query(Credential).filter(Credential.principal_id == p.id).first()
        assert cred is not None
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}
    finally:
        s.close()


def _add_device(session, device_id: str, *, ip: str = "10.0.9.51") -> Device:
    now = utcnow()
    d = Device(
        device_id=device_id,
        display_name=device_id,
        model_class=None,
        status=DeviceStatus.AVAILABLE,
        health_level=HealthLevel.L3,
        owner="owner@corp.com",
        source="mock",
        ip=ip,
        adb_port=5555,
        created_at=now,
        last_seen_at=now,
    )
    session.add(d)
    session.commit()
    return d


def test_web_apply_granted_occupies_device(client, session, db):
    _add_device(session, "dev_web_lease_a")
    cookies = _cookie(db, "user01@corp.com")

    r = client.post(
        "/api/leases/apply",
        cookies=cookies,
        json={"device_id": "dev_web_lease_a", "purpose": "人工测试", "expected_minutes": 60},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "GRANTED", body
    lease = body["lease"]
    assert lease["device_id"] == "dev_web_lease_a"
    assert lease["lease_id"]
    assert lease.get("adb_address")

    session.expire_all()
    d = session.get(Device, "dev_web_lease_a")
    assert d.status == DeviceStatus.OCCUPIED

    devices = client.get("/api/devices", cookies=cookies)
    assert devices.status_code == 200
    row = next(x for x in devices.json() if x["device_id"] == "dev_web_lease_a")
    assert row["status"] == DeviceStatus.OCCUPIED
    assert row["holder_email"] == "user01@corp.com"
    assert row["lease_id"] == lease["lease_id"]
    assert row["lease_state"] in LeaseState.ACTIVE
    assert row["lease_expires_at"]
    assert row["lease_is_mine"] is True

    detail = client.get("/api/devices/dev_web_lease_a", cookies=cookies)
    assert detail.status_code == 200, detail.text
    dbody = detail.json()
    assert dbody["holder_email"] == "user01@corp.com"
    assert dbody["lease_id"] == lease["lease_id"]
    assert dbody["lease_is_mine"] is True
    assert dbody["lease_expires_at"]
    assert dbody["lease_state"] in LeaseState.ACTIVE

    leases = client.get("/api/leases", cookies=cookies)
    assert leases.status_code == 200
    mine = [x for x in leases.json() if x["device_id"] == "dev_web_lease_a"]
    assert len(mine) == 1
    assert mine[0]["lease_id"] == lease["lease_id"]
    assert mine[0].get("agent_label", "").startswith("WebUI/")


def test_agent_cannot_steal_web_occupied_device(client, session, db):
    _add_device(session, "dev_web_lease_b", ip="10.0.9.52")
    cookies_a = _cookie(db, "user01@corp.com")

    r = client.post(
        "/api/leases/apply",
        cookies=cookies_a,
        json={"device_id": "dev_web_lease_b"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "GRANTED"

    # 用户 B / Agent 对同一设备 res_apply → 排队，拿不到
    agent_r = db.call("user02@corp.com", res_apply, target="dev_web_lease_b", purpose="agent抢")
    assert agent_r["state"] == "QUEUED", agent_r
    assert agent_r.get("ticket_id")

    session.expire_all()
    d = session.get(Device, "dev_web_lease_b")
    assert d.status == DeviceStatus.OCCUPIED
    active = (
        session.query(Lease)
        .filter(
            Lease.device_id == "dev_web_lease_b",
            Lease.state.in_(LeaseState.ACTIVE),
        )
        .all()
    )
    assert len(active) == 1
    assert active[0].principal.email == "user01@corp.com"


def test_web_release_restores_available(client, session, db):
    _add_device(session, "dev_web_lease_c", ip="10.0.9.53")
    cookies = _cookie(db, "user01@corp.com")

    r = client.post(
        "/api/leases/apply",
        cookies=cookies,
        json={"device_id": "dev_web_lease_c"},
    )
    assert r.status_code == 200, r.text
    lease_id = r.json()["lease"]["lease_id"]

    rel = client.post(f"/api/leases/{lease_id}/release", cookies=cookies)
    assert rel.status_code == 200, rel.text
    assert rel.json().get("ok") is True

    session.expire_all()
    d = session.get(Device, "dev_web_lease_c")
    assert d.status == DeviceStatus.AVAILABLE


def test_release_other_users_lease_forbidden(client, session, db):
    _add_device(session, "dev_web_lease_d", ip="10.0.9.54")
    cookies_a = _cookie(db, "user01@corp.com")
    cookies_b = _cookie(db, "user02@corp.com")

    r = client.post(
        "/api/leases/apply",
        cookies=cookies_a,
        json={"device_id": "dev_web_lease_d"},
    )
    assert r.status_code == 200, r.text
    lease_id = r.json()["lease"]["lease_id"]

    forbidden = client.post(f"/api/leases/{lease_id}/release", cookies=cookies_b)
    assert forbidden.status_code == 403, forbidden.text
    detail = forbidden.json().get("detail") or {}
    if isinstance(detail, dict):
        assert detail.get("code") == "FORBIDDEN"

    session.expire_all()
    d = session.get(Device, "dev_web_lease_d")
    assert d.status == DeviceStatus.OCCUPIED


def test_web_renew_own_lease(client, session, db):
    _add_device(session, "dev_web_lease_e", ip="10.0.9.55")
    cookies = _cookie(db, "user01@corp.com")

    r = client.post(
        "/api/leases/apply",
        cookies=cookies,
        json={"device_id": "dev_web_lease_e", "expected_minutes": 30},
    )
    assert r.status_code == 200, r.text
    lease = r.json()["lease"]
    lease_id = lease["lease_id"]
    before = lease["expires_at"]

    renew = client.post(
        f"/api/leases/{lease_id}/renew",
        cookies=cookies,
        json={"extra_minutes": 30},
    )
    assert renew.status_code == 200, renew.text
    body = renew.json()
    assert body["lease_id"] == lease_id
    assert body["expires_at"]
    assert body["expires_at"] >= before


def test_apply_requires_login(client, session):
    _add_device(session, "dev_web_lease_f", ip="10.0.9.56")
    r = client.post("/api/leases/apply", json={"device_id": "dev_web_lease_f"})
    assert r.status_code == 401


def test_web_apply_busy_409_no_queue_entry(client, session, db):
    """他人持有 → 409 DEVICE_BUSY，且不建 QueueEntry（与 agent apply 入队不同）。"""
    _add_device(session, "dev_web_busy", ip="10.0.9.57")
    cookies_a = _cookie(db, "user01@corp.com")
    cookies_b = _cookie(db, "user02@corp.com")

    r = client.post(
        "/api/leases/apply",
        cookies=cookies_a,
        json={"device_id": "dev_web_busy", "ttl_minutes": 5},
    )
    assert r.status_code == 200, r.text

    busy = client.post(
        "/api/leases/apply",
        cookies=cookies_b,
        json={"device_id": "dev_web_busy"},
    )
    assert busy.status_code == 409, busy.text
    detail = busy.json().get("detail") or {}
    assert isinstance(detail, dict)
    assert detail.get("code") == "DEVICE_BUSY"
    inner = detail.get("detail") or {}
    assert inner.get("holder") == "user01@corp.com"
    assert inner.get("expires_at")

    session.expire_all()
    queued = (
        session.query(QueueEntry)
        .filter(
            QueueEntry.target_type == "device",
            QueueEntry.target_code == "dev_web_busy",
        )
        .count()
    )
    assert queued == 0


def test_web_apply_ttl_strict_no_1_5x(client, session, db):
    """Web TTL 严格 min(ttl, max)，5 分钟不得被 1.5× 放大成 7。"""
    _add_device(session, "dev_web_ttl5", ip="10.0.9.58")
    cookies = _cookie(db, "user01@corp.com")
    r = client.post(
        "/api/leases/apply",
        cookies=cookies,
        json={"device_id": "dev_web_ttl5", "ttl_minutes": 5},
    )
    assert r.status_code == 200, r.text
    lease = r.json()["lease"]
    assert lease["ttl_minutes"] == 5
    assert lease["ttl_minutes"] != 7


def test_web_apply_idempotent_own_lease(client, session, db):
    """自己已持有 → 幂等返回原 lease，不新建。"""
    _add_device(session, "dev_web_idem", ip="10.0.9.59")
    cookies = _cookie(db, "user01@corp.com")
    r1 = client.post(
        "/api/leases/apply",
        cookies=cookies,
        json={"device_id": "dev_web_idem", "ttl_minutes": 5},
    )
    assert r1.status_code == 200, r1.text
    lease_id = r1.json()["lease"]["lease_id"]

    r2 = client.post(
        "/api/leases/apply",
        cookies=cookies,
        json={"device_id": "dev_web_idem", "ttl_minutes": 5},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["state"] == "GRANTED"
    assert r2.json()["lease"]["lease_id"] == lease_id

    session.expire_all()
    active = (
        session.query(Lease)
        .filter(
            Lease.device_id == "dev_web_idem",
            Lease.state.in_(LeaseState.ACTIVE),
        )
        .count()
    )
    assert active == 1
