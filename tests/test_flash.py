"""v0.14 固件：版本目录解析 / 异步刷机状态机 / 版本门禁（mock 子进程）。"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from app.constants import DeviceStatus, HealthLevel
from app.domain import flash
from app.mcp.server import (
    res_apply,
    res_firmware_status,
    res_flash,
    res_flash_report,
    res_flash_status,
    res_release,
)
from app.models import BuildServer, Device, FlashJob, Principal, ProductType
from app.remote_ssh import SshResult
from app.secret_crypto import encrypt_secret
from app.utils import new_device_id, utcnow


def _ensure_product(db, model: str = "QM102V", db_project_name: str | None = "esl2") -> None:
    """v0.17 起 seed.PRODUCT_TYPES=[]，flash 测试自行补机型字典行。"""
    session = db.session()
    try:
        pt = session.get(ProductType, model)
        if pt is None:
            session.add(
                ProductType(
                    type_code=model,
                    display_name=model,
                    has_screen=True,
                    has_led=False,
                    serial_count=0,
                    adb_port=5555,
                    enabled=True,
                    db_project_name=db_project_name,
                    created_at=utcnow(),
                )
            )
        elif db_project_name is not None and not pt.db_project_name:
            pt.db_project_name = db_project_name
        session.commit()
    finally:
        session.close()


def _make_device(db, model: str = "QM102V", firmware: str | None = None) -> str:
    _ensure_product(db, model=model)
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
    _ensure_product(db, db_project_name="esl2")
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
    _ensure_product(db, db_project_name="esl2")
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
    _ensure_product(db, db_project_name="esl2")
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
    _ensure_product(db, db_project_name="esl2")
    device_id = _make_device(db, firmware="1.0.0")
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    captured = {}

    def fake_execute(cmd, log_path, timeout, cwd=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
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
    _ensure_product(db, db_project_name="esl2")
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    r = db.call("user01@corp.com", res_flash, lease_id=lease_id, preserve_data=False)
    assert r["error"] == "NEED_CONFIRM_ERASE"

    captured = {}

    def fake_execute(cmd, log_path, timeout, cwd=None):
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
    _ensure_product(db, db_project_name="esl2")
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    def fake_execute(cmd, log_path, timeout, cwd=None):
        return 1, "download failed\nHTTP 500"

    monkeypatch.setattr(flash, "_execute", fake_execute)

    r = db.call("user01@corp.com", res_flash, lease_id=lease_id, version="130")
    done = _wait_status(db, "user01@corp.com", r["job_id"])
    assert done["status"] == "FAILED"
    assert "HTTP 500" in done["error"]


def test_flash_in_progress_blocks_duplicate(db, monkeypatch):
    _ensure_product(db, db_project_name="esl2")
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    gate = threading.Event()

    def blocking_execute(cmd, log_path, timeout, cwd=None):
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
    _ensure_product(db, db_project_name="esl2")
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    r = db.call("user02@corp.com", res_flash, lease_id=lease_id, version="130")
    assert r["error"] == "FORBIDDEN"

    r2 = db.call("user01@corp.com", res_flash, lease_id="L-not-exist", version="130")
    assert r2["error"] == "NOT_FOUND"


def test_firmware_status_semver(db):
    _ensure_product(db)
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
    _ensure_product(db)
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


# ---------- v0.53.2：per-job 工作目录隔离 ----------


def test_job_work_dir_distinct_per_job_id(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "flash_work_dir", str(tmp_path / "flash"))
    a = flash._job_work_dir("fj_aaa111")
    b = flash._job_work_dir("fj_bbb222")
    assert a != b
    assert a.name == "fj_aaa111"
    assert b.name == "fj_bbb222"
    assert a.parent == b.parent == (tmp_path / "flash" / "jobs").resolve()
    assert a.is_dir() and b.is_dir()


def test_execute_uses_passed_cwd(tmp_path):
    """_execute 必须以传入 cwd 为子进程工作目录（不依赖 dbxostest）。"""
    work = tmp_path / "job_cwd"
    work.mkdir()
    log_path = str(tmp_path / "job.log")
    cmd = ["python3", "-c", "import os,sys; sys.stdout.write(os.getcwd())"]
    rc, output = flash._execute(cmd, log_path, timeout=10, cwd=work)
    assert rc == 0
    assert Path(output.strip()).resolve() == work.resolve()


def test_run_job_removes_workdir_keeps_log(db, tmp_path, monkeypatch):
    """job 结束后工作目录删除、logs/<job_id>.log 保留（直接跑 _run_job，不依赖机型种子）。"""
    from app.config import settings
    from app.models import FlashJob

    flash_root = tmp_path / "flash"
    log_dir = flash_root / "logs"
    log_dir.mkdir(parents=True)
    monkeypatch.setattr(settings, "flash_work_dir", str(flash_root))
    monkeypatch.setattr(settings, "flash_log_dir", str(log_dir))

    job_id = "fj_iso001"
    log_path = str(log_dir / f"{job_id}.log")
    session = db.session()
    try:
        session.add(
            ProductType(
                type_code="ISO_PT",
                display_name="iso-test",
                has_screen=True,
                has_led=False,
                serial_count=0,
                adb_port=5555,
                enabled=True,
                db_project_name="esl2",
                created_at=utcnow(),
            )
        )
        device_id = new_device_id()
        session.add(
            Device(
                device_id=device_id,
                display_name=device_id,
                model_class="ISO_PT",
                owner="owner@corp.com",
                status=DeviceStatus.AVAILABLE,
                health_level=HealthLevel.L3,
                ip="10.1.2.9",
                adb_port=5555,
                firmware_version="1.0.0",
                source="mock",
                created_at=utcnow(),
            )
        )
        session.add(
            FlashJob(
                job_id=job_id,
                lease_id="L-iso-test",
                device_id=device_id,
                product_type="ISO_PT",
                version="130",
                preserve_data=True,
                status=flash.STATUS_QUEUED,
                log_path=log_path,
                created_at=utcnow(),
            )
        )
        session.commit()
    finally:
        session.close()

    seen = {}

    def fake_execute(cmd, log_path, timeout, cwd=None):
        seen["cwd"] = Path(cwd) if cwd is not None else None
        assert cwd is not None
        cwd_path = Path(cwd)
        (cwd_path / "downloads").mkdir(parents=True, exist_ok=True)
        (cwd_path / "downloads" / "CURRENT.tar.gz").write_bytes(b"pkg")
        (cwd_path / "tmp").mkdir(exist_ok=True)
        (cwd_path / "test_report").mkdir(exist_ok=True)
        Path(log_path).write_text("flash ok\n", encoding="utf-8")
        assert cwd_path.exists()
        return 0, "ok"

    monkeypatch.setattr(flash, "_execute", fake_execute)
    monkeypatch.setattr(flash, "_read_flashed_version", lambda ip, port: "V1.00.01.130")

    flash._run_job(job_id)

    job_cwd = flash_root / "jobs" / job_id
    assert seen["cwd"] is not None
    assert seen["cwd"].resolve() == job_cwd.resolve()
    assert not job_cwd.exists(), "job 工作目录应在 finally 中删除"
    assert Path(log_path).is_file()
    assert "flash ok" in Path(log_path).read_text(encoding="utf-8")

    session = db.session()
    try:
        job = session.get(FlashJob, job_id)
        assert job is not None
        assert job.status == flash.STATUS_SUCCEEDED
    finally:
        session.close()


def test_prune_stale_flash_job_dirs_and_tmp_dbxos(tmp_path, monkeypatch):
    """定时清理：过期 jobs/ 目录与 /tmp/dbxos_*；不删 logs、不碰非 dbxos_。"""
    import os
    import time as _time

    from app import cache_prune
    from app.config import settings

    flash_root = tmp_path / "flash"
    jobs = flash_root / "jobs"
    logs = flash_root / "logs"
    stale = jobs / "fj_stale"
    fresh = jobs / "fj_fresh"
    stale.mkdir(parents=True)
    fresh.mkdir(parents=True)
    (stale / "downloads").mkdir()
    (stale / "downloads" / "x.gz").write_bytes(b"old")
    (fresh / "downloads").mkdir()
    (fresh / "downloads" / "y.gz").write_bytes(b"new")
    logs.mkdir(parents=True)
    keep_log = logs / "fj_stale.log"
    keep_log.write_text("keep me\n", encoding="utf-8")

    old = _time.time() - 7 * 3600
    os.utime(stale, (old, old))
    os.utime(stale / "downloads", (old, old))
    os.utime(stale / "downloads" / "x.gz", (old, old))

    tmp_root = tmp_path / "tmp"
    tmp_root.mkdir()
    old_dbxos = tmp_root / "dbxos_old"
    new_dbxos = tmp_root / "dbxos_new"
    other = tmp_root / "other_stuff"
    old_dbxos.mkdir()
    new_dbxos.mkdir()
    other.mkdir()
    (old_dbxos / "a").write_text("x")
    (other / "b").write_text("y")
    os.utime(old_dbxos, (old, old))
    os.utime(old_dbxos / "a", (old, old))

    monkeypatch.setattr(settings, "flash_work_dir", str(flash_root))
    monkeypatch.setattr(settings, "flash_job_keep_hours", 6)
    monkeypatch.setattr(settings, "tmp_dbxos_keep_hours", 6)

    jr = cache_prune.prune_flash_job_dirs(dry_run=False)
    assert not stale.exists()
    assert fresh.exists()
    assert keep_log.is_file()
    assert jr["deleted_count"] == 1

    tr = cache_prune.prune_tmp_dbxos(dry_run=False, tmp_root=tmp_root)
    assert not old_dbxos.exists()
    assert new_dbxos.exists()
    assert other.exists()
    assert tr["deleted_count"] == 1


# ---------- v0.54：local / build_server 镜像来源 ----------


def _mk_build_server(db, email: str, host: str = "172.16.1.202", work_root: str = "OpenHarmony"):
    session = db.session()
    try:
        p = session.query(Principal).filter(Principal.email == email).one()
        row = BuildServer(
            server_id="bs_flash01",
            principal_id=p.id,
            name="bs-flash",
            host=host,
            port=22,
            username="yangxiongwei",
            auth_mode="agent",
            secret_encrypted=encrypt_secret("unused"),
            work_root=work_root,
            status="available",
            created_at=utcnow(),
        )
        session.add(row)
        session.commit()
        return row.server_id
    finally:
        session.close()


def test_flash_local_script_waiting_agent(db, monkeypatch):
    monkeypatch.setattr(
        "app.domain.flash.settings.flash_agent_timeout_seconds", 3600
    )
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    script = "/home/agent/images/qmimages/script.ini"

    r = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="local",
        image_mode="script",
        script_ini=script,
        image_name="local-build",
        image_sha256="abc123",
    )
    assert r.get("error") is None, r
    assert r["status"] == "WAITING_AGENT"
    assert r["mode"] == "agent_local"
    assert r["executor"] == "agent"
    assert r["device_ip"] == "10.1.2.3"
    assert r["adb_address"] == "10.1.2.3:5555"
    assert r["command_argv"] == [
        "xosadb2update",
        "-f",
        "-a",
        "-e",
        "data",
        "-i",
        "10.1.2.3",
        script,
    ]
    assert script in r["command"]
    assert "res_flash_report" in r["report_hint"]
    assert r["image"]["source"] == "local"
    assert r["image"]["mode"] == "script"
    assert r["image"]["script_ini"] == script
    assert r["image"]["sha256"] == "abc123"

    session = db.session()
    try:
        job = session.get(FlashJob, r["job_id"])
        assert job is not None
        assert job.status == "WAITING_AGENT"
        assert job.executor == "agent"
        assert job.image_meta["script_ini"] == script
        assert job.progress == 0
    finally:
        session.close()

    # WAITING_AGENT 占住租约，不能再开新任务
    blocked = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="local",
        image_mode="script",
        script_ini=script,
    )
    assert blocked["error"] == "FLASH_IN_PROGRESS"


def test_flash_local_partition_command(db, monkeypatch):
    monkeypatch.setattr(
        "app.domain.flash.settings.flash_agent_timeout_seconds", 3600
    )
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    img = "/data/out/boot.img"

    r = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="local",
        image_mode="partition",
        partition="boot",
        file_path=img,
        preserve_data=False,
        confirm_erase_data=True,
    )
    assert r.get("error") is None, r
    assert r["command_argv"] == [
        "xosadb2update",
        "-f",
        "-a",
        "-i",
        "10.1.2.3",
        "-p",
        "boot",
        img,
    ]
    assert "-e" not in r["command_argv"]
    assert r["image"]["partition"] == "boot"
    assert r["image"]["file_path"] == img


def test_flash_local_need_image(db):
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    r = db.call("user01@corp.com", res_flash, lease_id=lease_id, source="local")
    assert r["error"] == "NEED_IMAGE"
    r2 = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="local",
        image_mode="script",
    )
    assert r2["error"] == "NEED_IMAGE"


def test_flash_report_running_succeeded_and_conflict(db, monkeypatch):
    monkeypatch.setattr(
        "app.domain.flash.settings.flash_agent_timeout_seconds", 3600
    )
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    created = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="local",
        image_mode="script",
        script_ini="/tmp/script.ini",
    )
    job_id = created["job_id"]

    running = db.call(
        "user01@corp.com",
        res_flash_report,
        job_id=job_id,
        status="RUNNING",
        progress=40,
        log_tail="flashing...",
    )
    assert running["status"] == "RUNNING"
    assert running["progress"] == 40
    assert running["reported_at"]
    assert any("flashing" in line for line in running["log_tail"])

    ok = db.call(
        "user01@corp.com",
        res_flash_report,
        job_id=job_id,
        status="SUCCEEDED",
        progress=100,
    )
    assert ok["status"] == "SUCCEEDED"
    assert ok["progress"] == 100
    assert ok["finished_at"]

    # 重复终态幂等
    again = db.call(
        "user01@corp.com",
        res_flash_report,
        job_id=job_id,
        status="SUCCEEDED",
    )
    assert again["status"] == "SUCCEEDED"
    assert again.get("error") is None or "error" not in again or again.get("error") in (None, "")

    conflict = db.call(
        "user01@corp.com",
        res_flash_report,
        job_id=job_id,
        status="FAILED",
        error="nope",
    )
    assert conflict["error"] == "JOB_ALREADY_FINISHED"


def test_flash_report_forbidden_for_other_user(db, monkeypatch):
    monkeypatch.setattr(
        "app.domain.flash.settings.flash_agent_timeout_seconds", 3600
    )
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    created = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="local",
        image_mode="script",
        script_ini="/tmp/script.ini",
    )
    r = db.call(
        "user02@corp.com",
        res_flash_report,
        job_id=created["job_id"],
        status="SUCCEEDED",
    )
    assert r["error"] == "FORBIDDEN"


def test_flash_build_server_success(db, monkeypatch):
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    server_id = _mk_build_server(db, "user01@corp.com")
    script = "/home/yangxiongwei/OpenHarmony/out/qmimages/script.ini"
    calls = []

    def fake_run_remote(host, port, username, auth_mode, secret, remote_cmd, timeout=120):
        calls.append({"cmd": remote_cmd, "timeout": timeout, "secret": secret})
        # 绝不把 secret 写进断言快照以外的持久化；此处只确认调用存在
        assert secret is not None
        if "command -v xosadb2update" in remote_cmd or "__PREFLIGHT_OK__" in remote_cmd or "realpath" in remote_cmd:
            return SshResult(0, "__PREFLIGHT_OK__", "")
        if remote_cmd.startswith("xosadb2update") or "xosadb2update" in remote_cmd:
            return SshResult(0, "flash ok on build server\n", "")
        return SshResult(1, "", f"unexpected: {remote_cmd[:80]}")

    monkeypatch.setattr("app.remote_ssh.run_remote", fake_run_remote)
    monkeypatch.setattr(
        "app.domain.flash._read_flashed_version", lambda ip, port: "V1.00.01.200"
    )

    r = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="build_server",
        image_mode="script",
        script_ini=script,
        image_name="200",
    )
    assert r.get("error") is None, r
    assert r["mode"] == "build_server"
    assert r["executor"] == "build_server"
    assert r["status"] == "QUEUED"
    assert r["runner"] == {
        "server_id": server_id,
        "name": "bs-flash",
        "host": "172.16.1.202",
        "username": "yangxiongwei",
        "work_root": "OpenHarmony",
    }
    assert "secret" not in r["runner"]
    assert r["command_argv"][0] == "xosadb2update"
    assert script in r["command_argv"]

    done = _wait_status(db, "user01@corp.com", r["job_id"], timeout=5.0)
    assert done["status"] == "SUCCEEDED", done
    assert done["mode"] == "build_server"
    assert done["runner_id"] == server_id
    # 预检 + 执行至少两次
    assert len(calls) >= 2
    exec_calls = [c for c in calls if c["cmd"].startswith("xosadb2update")]
    assert exec_calls
    assert "10.1.2.3" in exec_calls[0]["cmd"]
    assert script in exec_calls[0]["cmd"]
    # 返回值与 status 不得泄露 secret
    st = db.call("user01@corp.com", res_flash_status, job_id=r["job_id"])
    blob = str(st) + str(r)
    assert "unused" not in blob


def test_flash_build_server_tool_missing(db, monkeypatch):
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    _mk_build_server(db, "user01@corp.com", host="172.16.1.193")

    def fake_run_remote(host, port, username, auth_mode, secret, remote_cmd, timeout=120):
        return SshResult(2, "__TOOL_MISSING__", "")

    monkeypatch.setattr("app.remote_ssh.run_remote", fake_run_remote)

    r = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="build_server",
        image_mode="script",
        script_ini="/home/yangxiongwei/OpenHarmony/out/script.ini",
    )
    assert r["error"] == "RUNNER_TOOL_MISSING"


def test_flash_build_server_path_outside_and_not_found(db, monkeypatch):
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    _mk_build_server(db, "user01@corp.com")

    # 相对路径：本地直接拒
    r = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="build_server",
        image_mode="script",
        script_ini="relative/script.ini",
    )
    assert r["error"] == "IMAGE_PATH_OUTSIDE_WORK_ROOT"

    def outside(host, port, username, auth_mode, secret, remote_cmd, timeout=120):
        return SshResult(4, "__OUTSIDE__=/tmp/evil.ini", "")

    monkeypatch.setattr("app.remote_ssh.run_remote", outside)
    r2 = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="build_server",
        image_mode="partition",
        partition="boot",
        file_path="/tmp/evil.img",
    )
    assert r2["error"] == "IMAGE_PATH_OUTSIDE_WORK_ROOT"

    def missing(host, port, username, auth_mode, secret, remote_cmd, timeout=120):
        return SshResult(3, "__NOT_FOUND__=/home/yangxiongwei/OpenHarmony/nope.img", "")

    monkeypatch.setattr("app.remote_ssh.run_remote", missing)
    r3 = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="build_server",
        image_mode="partition",
        partition="rootfs",
        file_path="/home/yangxiongwei/OpenHarmony/nope.img",
    )
    assert r3["error"] == "IMAGE_NOT_FOUND"


def test_flash_build_server_remote_failure(db, monkeypatch):
    device_id = _make_device(db)
    lease_id = _grant_lease(db, "user01@corp.com", device_id)
    _mk_build_server(db, "user01@corp.com")
    script = "/home/yangxiongwei/OpenHarmony/out/qmimages/script.ini"

    def fake_run_remote(host, port, username, auth_mode, secret, remote_cmd, timeout=120):
        if "__PREFLIGHT_OK__" in remote_cmd or "realpath" in remote_cmd:
            return SshResult(0, "__PREFLIGHT_OK__", "")
        return SshResult(1, "", "xosadb2update: device busy\n")

    monkeypatch.setattr("app.remote_ssh.run_remote", fake_run_remote)

    r = db.call(
        "user01@corp.com",
        res_flash,
        lease_id=lease_id,
        source="build_server",
        image_mode="script",
        script_ini=script,
    )
    assert r.get("error") is None, r
    done = _wait_status(db, "user01@corp.com", r["job_id"])
    assert done["status"] == "FAILED"
    assert "device busy" in (done["error"] or "")


def test_flash_db_source_returns_mode_server_db(db, monkeypatch):
    """source=db 默认行为不回归，并补充 mode/executor。"""
    _ensure_product(db, db_project_name="esl2")
    device_id = _make_device(db, firmware="1.0.0")
    lease_id = _grant_lease(db, "user01@corp.com", device_id)

    def fake_execute(cmd, log_path, timeout, cwd=None):
        Path(log_path).write_text("ok\n", encoding="utf-8")
        return 0, "ok"

    monkeypatch.setattr(flash, "_execute", fake_execute)
    monkeypatch.setattr(flash, "read_firmware_version", lambda ip, port: "V1.00.01.130")

    r = db.call("user01@corp.com", res_flash, lease_id=lease_id, version="130")
    assert r["status"] == "QUEUED"
    assert r["mode"] == "server_db"
    assert r["executor"] == "server"
    done = _wait_status(db, "user01@corp.com", r["job_id"])
    assert done["status"] == "SUCCEEDED"
    assert done["mode"] == "server_db"
    assert done["executor"] == "server"
