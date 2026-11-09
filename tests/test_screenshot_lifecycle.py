"""v0.24 截图生命周期纯函数/整合单测：不依赖真实设备。

覆盖：per-device prune 只保留最新 N、全局 retention/total_max prune、
释放时清理（含配置开关）、单条删除文件缺失不报错、统计、序列化、leasing 整合。
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from app import screenshot
from app import screenshot_lifecycle as lc
from app.models import AuditEvent, Device, Lease, Principal, ScreenshotRecord
from app.runtime_config import set_runtime_value
from app.security import AuthContext
from app.utils import new_id, utcnow


@pytest.fixture
def shot_dir(tmp_path, monkeypatch):
    """把截图目录指向临时目录，避免污染 app/var/screenshots。"""
    d = tmp_path / "shots"
    d.mkdir()
    monkeypatch.setattr(screenshot, "SCREENSHOT_DIR", d)
    return d


def _add_record(
    session,
    *,
    device_id: str = "dev_a",
    captured_at=None,
    bytes_: int = 100,
    file_name: str | None = None,
    shot_id: str | None = None,
    with_file: bool = True,
    shot_dir=None,
):
    """插入一条截图记录，可选在 shot_dir 下建同名 jpg 文件。"""
    captured_at = captured_at or utcnow()
    file_name = file_name or (new_id("shotfile_", 4) + ".jpg")
    rec = ScreenshotRecord(
        shot_id=shot_id or new_id("shot_", 8),
        device_id=device_id,
        file_name=file_name,
        bytes=bytes_,
        quality=60,
        rotate=0,
        float_abi="hard",
        bin="vxosdbg",
        created_by="tester",
        captured_at=captured_at,
    )
    session.add(rec)
    if with_file and shot_dir is not None:
        (shot_dir / file_name).write_bytes(b"\xff\xd8\xff" + b"0" * 64 + b"\xff\xd9")
    return rec


def _prune_audits(session) -> list[dict]:
    return [
        e.detail
        for e in session.query(AuditEvent).filter(AuditEvent.action == "screenshot_prune").all()
    ]


# ---------------- 2a / per-device prune ----------------

def test_prune_device_keeps_latest_n(db, shot_dir):
    s = db.session()
    now = utcnow()
    recs = []
    for i in range(5):
        r = _add_record(s, captured_at=now - timedelta(minutes=5 - i), shot_dir=shot_dir)
        recs.append(r)
    s.commit()
    files = [shot_dir / r.file_name for r in recs]

    deleted = lc.prune_device(s, "dev_a", actor="tester", max_per_device=2)

    assert deleted == 3
    remaining = s.query(ScreenshotRecord).filter_by(device_id="dev_a").all()
    assert {r.shot_id for r in remaining} == {recs[3].shot_id, recs[4].shot_id}
    # 最旧 3 个文件被删，最新 2 个仍在
    assert not files[0].exists() and not files[1].exists() and not files[2].exists()
    assert files[3].exists() and files[4].exists()
    assert any(d["count"] == 3 and d["reason"] == "per_device_limit" for d in _prune_audits(s))
    s.close()


def test_prune_device_under_limit_is_noop(db, shot_dir):
    s = db.session()
    for _ in range(2):
        _add_record(s, shot_dir=shot_dir)
    s.commit()

    assert lc.prune_device(s, "dev_a", max_per_device=5) == 0
    assert s.query(ScreenshotRecord).filter_by(device_id="dev_a").count() == 2
    assert _prune_audits(s) == []
    s.close()


def test_prune_device_reads_runtime_config(db, shot_dir):
    s = db.session()
    set_runtime_value(s, "screenshot_max_per_device", 2)
    s.commit()
    for i in range(4):
        _add_record(s, captured_at=utcnow() - timedelta(minutes=4 - i), shot_dir=shot_dir)
    s.commit()

    assert lc.prune_device(s, "dev_a") == 2  # 未显式传 max 时读在线配置
    assert s.query(ScreenshotRecord).filter_by(device_id="dev_a").count() == 2
    s.close()


# ---------------- 2b / 全局 prune ----------------

def test_prune_global_retention_days(db, shot_dir):
    s = db.session()
    set_runtime_value(s, "screenshot_retention_days", 1)
    set_runtime_value(s, "screenshot_total_max", 500)
    s.commit()
    old = utcnow() - timedelta(days=3)
    for _ in range(2):
        _add_record(s, captured_at=old, shot_dir=shot_dir)
    for _ in range(2):
        _add_record(s, captured_at=utcnow(), shot_dir=shot_dir)
    s.commit()

    result = lc.prune_global(s, actor="tester")

    assert result["retention_deleted"] == 2
    assert result["total_deleted"] == 0
    assert result["remaining"] == 2
    assert s.query(ScreenshotRecord).count() == 2
    assert any(d["count"] == 2 and d["reason"] == "retention_days" for d in _prune_audits(s))
    s.close()


def test_prune_global_total_max_oldest_first(db, shot_dir):
    s = db.session()
    set_runtime_value(s, "screenshot_retention_days", 3650)
    set_runtime_value(s, "screenshot_total_max", 3)
    s.commit()
    now = utcnow()
    recs = []
    for i in range(6):
        recs.append(_add_record(s, captured_at=now - timedelta(minutes=6 - i), shot_dir=shot_dir))
    s.commit()

    result = lc.prune_global(s, actor="tester")

    assert result["retention_deleted"] == 0
    assert result["total_deleted"] == 3
    assert result["remaining"] == 3
    kept = {r.shot_id for r in s.query(ScreenshotRecord).all()}
    assert kept == {recs[3].shot_id, recs[4].shot_id, recs[5].shot_id}  # 保留最新 3
    assert any(d["count"] == 3 and d["reason"] == "total_max" for d in _prune_audits(s))
    s.close()


# ---------------- 释放清理 / 单条删除 ----------------

def test_delete_device_screenshots(db, shot_dir):
    s = db.session()
    files = []
    for i in range(3):
        r = _add_record(s, device_id="dev_b", shot_dir=shot_dir)
        files.append(shot_dir / r.file_name)
    s.commit()

    assert lc.delete_device_screenshots(s, "dev_b", actor="admin@x", reason="release") == 3
    assert s.query(ScreenshotRecord).filter_by(device_id="dev_b").count() == 0
    assert not any(f.exists() for f in files)
    assert any(
        d["count"] == 3 and d["reason"] == "release" for d in _prune_audits(s)
    )
    s.close()


def test_delete_screenshot_missing_file_does_not_raise(db, shot_dir):
    s = db.session()
    r = _add_record(s, with_file=False)  # 记录存在但磁盘文件缺失
    s.commit()
    assert not (shot_dir / r.file_name).exists()

    assert screenshot.delete_screenshot(s, shot_id=r.shot_id) is True
    assert s.query(ScreenshotRecord).filter_by(shot_id=r.shot_id).count() == 0
    # 重复删除 / 不存在的 shot_id 返回 False，不抛
    assert screenshot.delete_screenshot(s, shot_id=r.shot_id) is False
    s.close()


def test_delete_screenshot_by_file_name(db, shot_dir):
    s = db.session()
    r = _add_record(s, shot_dir=shot_dir)
    s.commit()
    assert screenshot.delete_screenshot(s, file_name=r.file_name, actor="admin@x") is True
    assert not (shot_dir / r.file_name).exists()
    assert any(
        d["count"] == 1 and d["reason"] == "manual_delete"
        for d in _prune_audits(s)
    )  # 单删同样写 audit
    s.close()


# ---------------- 列举 / 统计 / 序列化 ----------------

def test_list_screenshots_filter_and_order(db, shot_dir):
    s = db.session()
    now = utcnow()
    _add_record(s, device_id="dev_a", captured_at=now - timedelta(minutes=2), shot_dir=shot_dir)
    newest = _add_record(s, device_id="dev_a", captured_at=now, shot_dir=shot_dir)
    _add_record(s, device_id="dev_b", captured_at=now, shot_dir=shot_dir)
    s.commit()

    rows = screenshot.list_screenshots(s, device_id="dev_a")
    assert len(rows) == 2
    assert rows[0]["shot_id"] == newest.shot_id       # 最新在前
    assert all(r["device_id"] == "dev_a" for r in rows)
    assert rows[0]["captured_at"].endswith("+00:00")  # UTC 偏移，JS 可转本地
    s.close()


def test_stats(db, shot_dir):
    s = db.session()
    _add_record(s, device_id="dev_a", bytes_=100, shot_dir=shot_dir)
    _add_record(s, device_id="dev_a", bytes_=200, shot_dir=shot_dir)
    _add_record(s, device_id="dev_b", bytes_=300, shot_dir=shot_dir)
    s.commit()

    st = lc.stats(s)
    assert st == {"count": 3, "total_bytes": 600, "devices": 2}
    s.close()


# ---------------- leasing._reclaim 整合：清理失败绝不影响释放 ----------------

def _seed_lease_with_screenshots(s, shot_dir, n=3, cleanup_enabled=True):
    now = utcnow()
    p = Principal(
        email="rel_%s@test.local" % new_id("", 4), name="rel", quota=2,
        is_admin=False, status="active", created_at=now,
    )
    s.add(p)
    s.flush()
    dev = Device(
        device_id="dev_rel", owner="tester", status="occupied",
        health_level="L3", created_at=now,
    )
    s.add(dev)
    s.flush()
    lease = Lease(
        lease_id="L-rel", device_id="dev_rel", principal_id=p.id,
        ttl_minutes=90, state="GRANTED", created_at=now, expires_at=now,
    )
    s.add(lease)
    paths = []
    for i in range(n):
        r = _add_record(s, device_id="dev_rel", shot_dir=shot_dir)
        paths.append(shot_dir / r.file_name)
    set_runtime_value(s, "screenshot_cleanup_on_release", cleanup_enabled)
    s.commit()
    return AuthContext(principal=p, credential=None, email=p.email, is_admin=True), paths


def test_release_cleans_screenshots_and_still_succeeds(db, shot_dir):
    from app.domain import leasing

    s = db.session()
    auth, paths = _seed_lease_with_screenshots(s, shot_dir)
    assert s.query(ScreenshotRecord).filter_by(device_id="dev_rel").count() == 3

    res = leasing.release(s, auth, "L-rel", reason="done")

    assert res["ok"] is True
    assert s.get(Lease, "L-rel").state == "RELEASED"
    assert s.query(ScreenshotRecord).filter_by(device_id="dev_rel").count() == 0
    assert not any(p.exists() for p in paths)
    assert any(
        d["count"] == 3 and d["reason"] == "release" for d in _prune_audits(s)
    )
    s.close()


def test_release_keeps_screenshots_when_disabled(db, shot_dir):
    from app.domain import leasing

    s = db.session()
    auth, paths = _seed_lease_with_screenshots(s, shot_dir, cleanup_enabled=False)
    res = leasing.release(s, auth, "L-rel", reason="done")

    assert res["ok"] is True
    assert s.query(ScreenshotRecord).filter_by(device_id="dev_rel").count() == 3
    assert all(p.exists() for p in paths)
    s.close()


# ---------------- capture_screenshot 成功路径（mock adb 层，验证记录入库/rotate/自动 prune） ----------------

def test_capture_writes_record_and_passes_rotate(db, shot_dir, monkeypatch):
    """无真实设备：mock 掉 adb 层，验证截图成功后写记录、rotate 透传、自动 per-device prune。"""
    s = db.session()
    s.add(Device(
        device_id="dev_mock", owner="tester", status="available", health_level="L3",
        ip="172.16.10.246", adb_port=5555, created_at=utcnow(),
    ))
    s.commit()

    def fake_capture_once(addr, remote_dir, bin_name, quality, rotate,
                          framebuffer, token, local_path, timeout):
        Path(local_path).write_bytes(b"\xff\xd8\xff" + b"0" * 128 + b"\xff\xd9")
        return "ok", True

    monkeypatch.setattr(screenshot, "_connect", lambda *a, **k: True)
    monkeypatch.setattr(screenshot, "_disconnect", lambda *a, **k: None)
    monkeypatch.setattr(screenshot, "_resolve_float_abi", lambda *a, **k: ("hard", "cpuinfo"))
    monkeypatch.setattr(screenshot, "_capture_once", fake_capture_once)

    r = screenshot.capture_screenshot(
        device_id="dev_mock", quality=70, rotate=90, session=s, created_by="tester"
    )

    assert r["rotate"] == 90 and r["quality"] == 70
    assert r["float_abi"] == "hard" and r["bin"] == "vxosdbg"
    assert r["bytes"] == 133
    rec = s.query(ScreenshotRecord).filter_by(file_name=r["file_name"]).first()
    assert rec is not None
    assert rec.rotate == 90 and rec.quality == 70 and rec.created_by == "tester"
    assert rec.device_id == "dev_mock" and rec.float_abi == "hard" and rec.bin == "vxosdbg"
    assert (shot_dir / r["file_name"]).is_file()

    # 自动 prune：默认每台 10 张，连续 12 次后只保留最新 10 条
    for _ in range(11):
        screenshot.capture_screenshot(
            device_id="dev_mock", quality=60, rotate=0, session=s, created_by="tester"
        )
    assert s.query(ScreenshotRecord).filter_by(device_id="dev_mock").count() == 10
    s.close()
