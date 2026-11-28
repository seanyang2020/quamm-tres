"""PT9 P3：POST /api/devices/{id}/adb/exec — 无真机，全 monkeypatch。"""
from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.constants import DeviceStatus, HealthLevel
from app.domain import adb_exec as adb_exec_svc
from app.domain.catalog import DomainError
from app.migrations import migrate
from app.models import Credential, Device, Principal
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


def _cookie(db, *, admin: bool):
    s = db.session()
    try:
        if admin:
            cred = s.query(Credential).filter_by(role="admin").first()
            if cred is None:
                cred = s.query(Credential).first()
                assert cred is not None
                p = s.get(Principal, cred.principal_id)
                p.is_admin = True
                cred.role = "admin"
                s.commit()
        else:
            cred = s.query(Credential).filter_by(role="user").first()
            if cred is None:
                cred = s.query(Credential).first()
                assert cred is not None
                p = s.get(Principal, cred.principal_id)
                p.is_admin = False
                cred.role = "user"
                s.commit()
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}, cred
    finally:
        s.close()


def _add_device(session, device_id: str, *, ip: str | None = "10.0.9.9") -> Device:
    now = utcnow()
    d = Device(
        device_id=device_id,
        display_name=device_id,
        model_class=None,
        status=DeviceStatus.AVAILABLE,
        health_level=HealthLevel.L3,
        owner="t",
        source="mock",
        ip=ip,
        adb_port=5555,
        created_at=now,
        last_seen_at=now,
    )
    session.add(d)
    session.commit()
    return d


def test_adb_no_endpoint(session):
    _add_device(session, "dev_adb_noep", ip=None)
    with pytest.raises(DomainError) as ei:
        adb_exec_svc.exec_shell(
            session,
            "dev_adb_noep",
            command="id",
            by="web:admin@t",
            email="admin@t",
            is_admin=True,
        )
    assert ei.value.code == "ADB_NO_ENDPOINT"


def test_adb_not_allowed_non_admin_no_lease(client, session, db):
    _add_device(session, "dev_adb_nolease")
    cookies, _ = _cookie(db, admin=False)
    r = client.post(
        "/api/devices/dev_adb_nolease/adb/exec",
        cookies=cookies,
        json={"command": "id"},
    )
    assert r.status_code == 403, r.text
    body = r.json()
    detail = body.get("detail") or body
    code = detail.get("code") if isinstance(detail, dict) else None
    assert code == "ADB_NOT_ALLOWED"


def test_adb_admin_no_lease_rejected_on_web(client, session, db):
    """Web 路径 allow_admin_bypass=False：admin 无租约也被拒。"""
    _add_device(session, "dev_adb_admin_nolease", ip="10.0.9.91")
    cookies, _ = _cookie(db, admin=True)
    r = client.post(
        "/api/devices/dev_adb_admin_nolease/adb/exec",
        cookies=cookies,
        json={"command": "id"},
    )
    assert r.status_code == 403, r.text
    detail = r.json().get("detail") or {}
    code = detail.get("code") if isinstance(detail, dict) else None
    assert code == "ADB_NOT_ALLOWED"


def test_adb_with_web_lease_ok(client, session, db, monkeypatch):
    """持有 Web 租约后 adb exec 可通过（admin）。"""
    _add_device(session, "dev_adb_leased", ip="10.0.9.92")
    cookies, _ = _cookie(db, admin=True)

    apply_r = client.post(
        "/api/leases/apply",
        cookies=cookies,
        json={"device_id": "dev_adb_leased", "ttl_minutes": 5},
    )
    assert apply_r.status_code == 200, apply_r.text

    def fake_run(args, timeout_s):
        if args[:1] == ["connect"]:
            return 0, "connected", ""
        return 0, "uid=0(root)", ""

    monkeypatch.setattr(adb_exec_svc, "_run_adb", fake_run)
    r = client.post(
        "/api/devices/dev_adb_leased/adb/exec",
        cookies=cookies,
        json={"command": "id"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("result") == "ok"
    assert "uid=0" in (body.get("stdout") or "")


def test_adb_confirm_required_no_execute(session, monkeypatch):
    _add_device(session, "dev_adb_danger")
    calls: list[list[str]] = []

    def fake_run(args, timeout_s):
        calls.append(list(args))
        return 0, "ok", ""

    monkeypatch.setattr(adb_exec_svc, "_run_adb", fake_run)
    with pytest.raises(DomainError) as ei:
        adb_exec_svc.exec_shell(
            session,
            "dev_adb_danger",
            command="reboot",
            confirm=False,
            by="web:admin@t",
            email="admin@t",
            is_admin=True,
        )
    assert ei.value.code == "ADB_CONFIRM_REQUIRED"
    assert calls == []


def test_adb_exec_ok_stdout_truncated(session, monkeypatch):
    _add_device(session, "dev_adb_ok")
    big = "X" * (adb_exec_svc.ADB_OUTPUT_LIMIT + 100)

    def fake_run(args, timeout_s):
        if args[:1] == ["connect"]:
            return 0, f"connected to {args[1]}", ""
        return 0, big, ""

    monkeypatch.setattr(adb_exec_svc, "_run_adb", fake_run)
    out = adb_exec_svc.exec_shell(
        session,
        "dev_adb_ok",
        command="cat /proc/version",
        by="web:admin@t",
        email="admin@t",
        is_admin=True,
    )
    assert out["result"] == "ok"
    assert out["rc"] == 0
    assert out["truncated"] is True
    assert len(out["stdout"]) == adb_exec_svc.ADB_OUTPUT_LIMIT
    assert out["stdout"].startswith("X")


def test_adb_timeout(session, monkeypatch):
    _add_device(session, "dev_adb_to")

    def fake_run(args, timeout_s):
        if args[:1] == ["connect"]:
            return 0, "already connected", ""
        return 124, "", "adb 超时"

    monkeypatch.setattr(adb_exec_svc, "_run_adb", fake_run)
    with pytest.raises(DomainError) as ei:
        adb_exec_svc.exec_shell(
            session,
            "dev_adb_to",
            command="sleep 999",
            timeout_ms=1000,
            by="web:admin@t",
            email="admin@t",
            is_admin=True,
        )
    assert ei.value.code == "ADB_TIMEOUT"
