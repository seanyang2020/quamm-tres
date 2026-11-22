"""t976：客户端安装包上传 / 列表 / 下载计数 / 删除权限 / 413 / 扩展名 / 路径穿越 / 迁移幂等。"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app import web_auth
from app.migrations import migrate
from app.models import AuditEvent, Credential, Principal, WlPackage


@pytest.fixture
def client(db, tmp_path, monkeypatch):
    pkg_root = tmp_path / "pkgdata"
    pkg_root.mkdir()
    monkeypatch.setattr("app.config.settings.data_dir", str(pkg_root))
    monkeypatch.setattr("app.domain.packages.settings.data_dir", str(pkg_root))
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


@pytest.fixture
def user_cookie(db):
    s = db.session()
    try:
        user = s.query(Principal).filter_by(is_admin=False).first()
        assert user is not None
        cred = s.query(Credential).filter_by(principal_id=user.id).first()
        assert cred is not None
        return {web_auth.SESSION_COOKIE: web_auth.make_session(cred.id)}
    finally:
        s.close()


def _zip_bytes(payload: bytes = b"PK\x03\x04fake-zip-body") -> bytes:
    return payload


def _upload(client, cookies, *, filename="elsb-0.33.0.zip", version="0.33.0",
            platform="Windows", name="elsb", channel="prod", notes="t",
            content=None):
    data = content if content is not None else _zip_bytes()
    return client.post(
        "/api/packages",
        cookies=cookies,
        data={
            "name": name,
            "version": version,
            "platform": platform,
            "channel": channel,
            "notes": notes,
        },
        files={"file": (filename, io.BytesIO(data), "application/zip")},
    )


def test_upload_list_download_count_delete(client, db, admin_cookie, user_cookie):
    r = _upload(client, admin_cookie)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "0.33.0"
    assert body["platform"] == "Windows"
    assert body["sha256"]
    assert body["size_bytes"] > 0
    assert body["download_url"].endswith(f"/api/packages/{body['id']}/download")
    pkg_id = body["id"]

    # 重复 → 409
    r2 = _upload(client, admin_cookie)
    assert r2.status_code == 409, r2.text

    # 列表：登录可读
    lst = client.get("/api/packages", cookies=user_cookie)
    assert lst.status_code == 200
    items = lst.json()["items"]
    assert any(i["id"] == pkg_id for i in items)

    # 未登录 401
    assert client.get("/api/packages").status_code == 401

    # 下载 + 计数
    d1 = client.get(f"/api/packages/{pkg_id}/download", cookies=user_cookie)
    assert d1.status_code == 200
    assert "attachment" in d1.headers.get("content-disposition", "").lower()
    assert d1.content.startswith(b"PK")

    s = db.session()
    try:
        pkg = s.get(WlPackage, pkg_id)
        assert pkg.download_count == 1
        ev = (
            s.query(AuditEvent)
            .filter_by(action="comres_package_download")
            .order_by(AuditEvent.event_id.desc())
            .first()
        )
        assert ev is not None
        assert "email" in (ev.detail or {}) or "@" in (ev.actor or "")
    finally:
        s.close()

    # user 删 403；admin 删 200
    assert client.delete(f"/api/packages/{pkg_id}", cookies=user_cookie).status_code == 403
    assert client.delete(f"/api/packages/{pkg_id}", cookies=admin_cookie).status_code == 200
    assert client.get(f"/api/packages/{pkg_id}/download", cookies=user_cookie).status_code == 404


def test_user_upload_forbidden(client, user_cookie):
    r = _upload(client, user_cookie, version="9.9.9")
    assert r.status_code == 403


def test_bad_extension(client, admin_cookie):
    r = _upload(client, admin_cookie, filename="evil.txt", version="1.0.1")
    assert r.status_code == 400
    detail = r.json().get("detail") or {}
    assert "PACKAGE_BAD_EXT" in str(detail) or "扩展名" in str(detail)


def test_path_traversal_filename(client, admin_cookie):
    r = _upload(
        client, admin_cookie,
        filename="../../etc/passwd.zip",
        version="1.0.2",
    )
    assert r.status_code == 400
    detail = r.json().get("detail") or {}
    assert "PACKAGE_BAD_FILENAME" in str(detail) or "路径" in str(detail)


def test_too_large_413(client, admin_cookie, monkeypatch):
    monkeypatch.setattr("app.config.settings.package_max_bytes", 64)
    monkeypatch.setattr("app.domain.packages.settings.package_max_bytes", 64)
    r = _upload(
        client, admin_cookie,
        version="1.0.3",
        content=b"x" * 128 + b".zip-pad",
    )
    assert r.status_code == 413, r.text


def test_migrate_idempotent(db):
    s = db.session()
    try:
        migrate(s)
        migrate(s)  # 二次无报错
        from sqlalchemy import inspect
        assert "wl_package" in inspect(s.get_bind()).get_table_names()
    finally:
        s.close()


def test_set_channel(client, admin_cookie, user_cookie):
    r = _upload(client, admin_cookie, version="2.0.0", channel="beta")
    assert r.status_code == 200
    pkg_id = r.json()["id"]
    assert client.put(
        f"/api/packages/{pkg_id}/channel",
        cookies=user_cookie,
        json={"channel": "prod"},
    ).status_code == 403
    ok = client.put(
        f"/api/packages/{pkg_id}/channel",
        cookies=admin_cookie,
        json={"channel": "prod"},
    )
    assert ok.status_code == 200
    assert ok.json()["channel"] == "prod"
    client.delete(f"/api/packages/{pkg_id}", cookies=admin_cookie)
