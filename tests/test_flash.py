"""v0.14 固件：版本目录解析 / 异步刷机状态机 / 版本门禁（mock 子进程）。"""
from __future__ import annotations

import threading
import time

from app.constants import DeviceStatus, HealthLevel
from app.domain import flash
from app.mcp.server import (
    res_apply,
    res_firmware_status,
    res_flash,
    res_flash_status,
    res_release,
)
from app.models import Device, ProductType
from app.utils import new_device_id, utcnow


def _make_device(db, model: str = "QM102V", firmware: str | None = None) -> str:
    session = db.session()
    try:
        device_id = new_device_id()
        session.add(
            Device(
                device_id=device_id,
                display_name=device_id,
                model_class=model,
                owner="owner@corp.com",
                status=DeviceStatus.AVAILABLE,
                health_level=HealthLevel.L3,
                ip="10.1.2.3",
                adb_port=5555,
                firmware_version=firmware,
                source="mock",
                created_at=utcnow(),
            )
        )
        session.commit()
        return device_id
    finally:
        session.close()


def _grant_lease(db, email: str, device_id: str) -> str:
    r = db.call(email, res_apply, device_id=device_id, purpose="flash-test")
    assert r["state"] == "GRANTED", r
    return r["lease"]["lease_id"]


def _wait_status(db, email: str, job_id: str, terminal=("SUCCEEDED", "FAILED"), timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = db.call(email, res_flash_status, job_id=job_id)
        if r.get("status") in terminal:
            return r
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} 未在 {timeout}s 内结束: {r}")


def test_firmware_versions_parse_and_sort(db, monkeypatch):
    session = db.session()
    try:
        pt = session.get(ProductType, "QM102V")
        pt.db_project_name = "esl2"
        session.commit()
    finally:
        session.close()

    html = """
    <a href="100/">100/</a>   01-Jan-2026 10:00       -
    <a href="130/">130/</a>   08-Nov-2026 19:01       -
    <a href="../">../</a>
    <a href="125/">125/</a>   05-Nov-2026 08:30       -
    """
    monkeypatch.setattr(flash, "_http_get", lambda url, timeout=None: html)

    from app.mcp.server import res_firmware_versions

    r = db.call("user01@corp.com", res_firmware_versions, product_type="QM102V", limit=2)
    assert r["db_project_name"] == "esl2"
    assert [v["version"] for v in r["versions"]] == ["130", "125"]
    assert r["versions"][0]["url"].endswith("/esl2/130/")
    assert r["versions"][0]["build_time"] == "08-Nov-2026 19:01"


def test_firmware_versions_no_project(db):
    session = db.session()
    try:
        session.get(ProductType, "QM102V").db_project_name = None
        session.commit()
    finally:
        session.close()
    from app.mcp.server import res_firmware_versions

    r = db.call("user01@corp.com", res_firmware_versions, product_type="QM102V")
    assert r["error"] == "PRODUCT_NO_DB_PROJECT"


def test_firmware_versions_source_unavailable(db, monkeypatch):
    session = db.session()
    try:
        session.get(ProductType, "QM102V").db_project_name = "esl2"
        session.commit()
    finally:
        session.close()

    import urllib.error

    def boom(url, timeout=None):
        raise urllib.error.URLError("unreachable")

    monkeypatch.setattr(flash, "_http_get", boom)
    from app.mcp.server import res_firmware_versions

    r = db.call("user01@corp.com", res_firmware_versions, product_type="QM102V")
    assert r["error"] == "SOURCE_UNAVAILABLE"


def test_flash_success_updates_device(db, monkeypatch):
    session = db.session()
    try:
        session.get(ProductType, "QM102V").db_project_name = "esl2"
        session.commit()
    finally:
        session.close()

    device_id = _make_device(db, firmware="1.0.0")
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    captured = {}

    def fake_execute(cmd, log_path, timeout):
        captured["cmd"] = cmd
        with open(log_path, "w", encoding="utf-8") as fh:
            fh.write("download ok\nflash ok\n")
        return 0, "ok"

    monkeypatch.setattr(flash, "_execute", fake_execute)
    monkeypatch.setattr(flash, "read_firmware_version", lambda ip, port: "V1.00.01.130")

    r = db.call("user01@corp.com", res_flash, lease_id=lease_id, version="130")
    assert r["job_id"] and r["status"] == "QUEUED"
    assert r["adb_address"] == "10.1.2.3:5555"

    done = _wait_status(db, "user01@corp.com", r["job_id"])
    assert done["status"] == "SUCCEEDED", done
    assert done["log_tail"][-1] == "flash ok"
    assert done["error"] is None

    # preserve_data 默认开：命令包含 -e data，且版本/工程正确
    cmd = captured["cmd"]
    assert cmd[cmd.index("-t") + 1] == "esl2"
    assert cmd[cmd.index("-i") + 1] == "10.1.2.3"
    assert cmd[cmd.index("-v") + 1] == "130"
    update_arg = next(a for a in cmd if a.startswith("--update-args="))
    assert update_arg == "--update-args=-f -a -e data"

    session = db.session()
    try:
        assert session.get(Device, device_id).firmware_version == "V1.00.01.130"
    finally:
        session.close()


def test_flash_erase_requires_confirm_and_drops_e_data(db, monkeypatch):
    session = db.session()
    try:
        session.get(ProductType, "QM102V").db_project_name = "esl2"
        session.commit()
    finally:
        session.close()

    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    r = db.call("user01@corp.com", res_flash, lease_id=lease_id, preserve_data=False)
    assert r["error"] == "NEED_CONFIRM_ERASE"

    captured = {}

    def fake_execute(cmd, log_path, timeout):
        captured["cmd"] = cmd
        return 0, ""

    monkeypatch.setattr(flash, "_execute", fake_execute)
    monkeypatch.setattr(flash, "read_firmware_version", lambda ip, port: None)
    monkeypatch.setattr(flash, "_read_flashed_version", lambda ip, port: None)

    r = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        version="130",
        preserve_data=False,
        confirm_erase_data=True,
    )
    done = _wait_status(db, "user01@corp.com", r["job_id"])
    assert done["status"] == "SUCCEEDED"
    update_arg = next(a for a in captured["cmd"] if a.startswith("--update-args="))
    assert "-e data" not in update_arg


def test_flash_failure_marks_failed(db, monkeypatch):
    session = db.session()
    try:
        session.get(ProductType, "QM102V").db_project_name = "esl2"
        session.commit()
    finally:
        session.close()

    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    def fake_execute(cmd, log_path, timeout):
        return 1, "download failed\nHTTP 500"

    monkeypatch.setattr(flash, "_execute", fake_execute)

    r = db.call("user01@corp.com", res_flash, lease_id=lease_id, version="130")
    done = _wait_status(db, "user01@corp.com", r["job_id"])
    assert done["status"] == "FAILED"
    assert "HTTP 500" in done["error"]


def test_flash_in_progress_blocks_duplicate(db, monkeypatch):
    session = db.session()
    try:
        session.get(ProductType, "QM102V").db_project_name = "esl2"
        session.commit()
    finally:
        session.close()

    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    gate = threading.Event()

    def blocking_execute(cmd, log_path, timeout):
        gate.wait(3)
        return 0, ""

    monkeypatch.setattr(flash, "_execute", blocking_execute)
    monkeypatch.setattr(flash, "read_firmware_version", lambda ip, port: "1.0.1")

    first = db.call("user01@corp.com", res_flash, lease_id=lease_id, version="130")
    second = db.call("user01@corp.com", res_flash, lease_id=lease_id, version="131")
    assert second["error"] == "FLASH_IN_PROGRESS"
    assert second["job_id"] == first["job_id"]

    gate.set()
    done = _wait_status(db, "user01@corp.com", first["job_id"])
    assert done["status"] == "SUCCEEDED"


def test_flash_requires_own_granted_lease(db, monkeypatch):
    session = db.session()
    try:
        session.get(ProductType, "QM102V").db_project_name = "esl2"
        session.commit()
    finally:
        session.close()

    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    r = db.call("user02@corp.com", res_flash, lease_id=lease_id, version="130")
    assert r["error"] == "FORBIDDEN"

    r2 = db.call("user01@corp.com", res_flash, lease_id="L-not-exist", version="130")
    assert r2["error"] == "NOT_FOUND"


def test_firmware_status_semver(db):
    session = db.session()
    try:
        pt = session.get(ProductType, "QM102V")
        pt.required_major = 1
        pt.required_minor = 2
        pt.required_patch = 5
        session.commit()
    finally:
        session.close()

    ok_id = _make_device(db, firmware="V1.2.5")
    old_minor_id = _make_device(db, firmware="V1.1.9")
    old_major_id = _make_device(db, firmware="V0.9.9")

    ok = db.call("user01@corp.com", res_firmware_status, device_id=ok_id)
    assert ok["state"] == "OK" and ok["upgrade_needed"] is False

    minor = db.call("user01@corp.com", res_firmware_status, device_id=old_minor_id)
    assert minor["state"] == "UPGRADE_REQUIRED" and minor["upgrade_needed"] is True
    assert minor["recommendation"]

    major = db.call("user01@corp.com", res_firmware_status, device_id=old_major_id)
    assert major["state"] == "INCOMPATIBLE" and major["upgrade_needed"] is True


def test_apply_gate_skips_incompatible_and_hints_patch(db):
    session = db.session()
    try:
        pt = session.get(ProductType, "QM102V")
        pt.required_major = 1
        pt.required_minor = 2
        pt.required_patch = 5
        session.commit()
    finally:
        session.close()

    old_id = _make_device(db, firmware="V1.1.9")
    r = db.call("user01@corp.com", res_apply, device_id=old_id)
    assert r["error"] == "NO_COMPATIBLE_DEVICE"

    # minor 满足、patch 落后：照常分配，只带 hint
    patch_id = _make_device(db, firmware="V1.2.1")
    r2 = db.call("user01@corp.com", res_apply, device_id=patch_id)
    assert r2["state"] == "GRANTED", r2
    assert "firmware_hint" in r2
    db.call("user01@corp.com", res_release, lease_id=r2["lease"]["lease_id"])

    # 未知版本：警告但分配
    unknown_id = _make_device(db, firmware=None)
    r3 = db.call("user01@corp.com", res_apply, device_id=unknown_id)
    assert r3["state"] == "GRANTED"
    assert r3.get("firmware_warning") == "UNKNOWN_VERSION"
    db.call("user01@corp.com", res_release, lease_id=r3["lease"]["lease_id"])

    # 同机型池：按创建顺序先遇到旧设备（跳过），选到兼容设备
    r4 = db.call("user02@corp.com", res_apply, product_type="QM102V")
    assert r4["state"] == "GRANTED", r4
    assert r4["lease"]["device_id"] == patch_id
