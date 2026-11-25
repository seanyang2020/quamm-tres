"""固件下载 / 刷机（v0.14）。

复用现成工具，不重新发明刷写协议：
- 版本来源：HTTP 目录 `http://172.16.1.201/Daily/XOS/<db_project_name>/`（dbxostest 同一源）
- 刷机执行：`python3 dbxostest.py -t <project> -i <ip> -v <version> -u "-f -a -e data" ...`
  （dbxostest 内部调用 xosadb2update；preserve_data=True 必带 `-e data`）

刷机 5-15 分钟，必须异步：MCP 调用只落一条 flash_job（QUEUED）并起后台线程，
线程内 QUEUED -> RUNNING -> SUCCEEDED / FAILED，状态与日志全部落库/落盘，
进程重启后仍可用 res_flash_status 查历史。

版本策略（semver X.Y.Z）：
- X < required_major：INCOMPATIBLE，res_apply 不分配
- Y < required_minor：UPGRADE_REQUIRED，res_apply 不分配
- Z < required_patch：PATCH_BEHIND，只在返回里提示，不阻塞
- 版本为空 / 解析失败：UNKNOWN_VERSION，警告但仍分配
required_* 为 0 表示不强制。
"""
from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.constants import LeaseState
from app.db import SessionLocal
from app.domain.catalog import DomainError
from app.domain.prober_real import read_firmware_version
from app.models import BuildServer, Device, FlashJob, Lease, ProductType
from app.security import AuthContext
from app.utils import new_id, utcnow

logger = logging.getLogger(__name__)

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_WAITING_AGENT = "WAITING_AGENT"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"

SOURCE_DB = "db"
SOURCE_LOCAL = "local"
SOURCE_BUILD_SERVER = "build_server"
_VALID_SOURCES = (SOURCE_DB, SOURCE_LOCAL, SOURCE_BUILD_SERVER)

EXECUTOR_SERVER = "server"
EXECUTOR_AGENT = "agent"
EXECUTOR_BUILD_SERVER = "build_server"

MODE_SERVER_DB = "server_db"
MODE_AGENT_LOCAL = "agent_local"
MODE_BUILD_SERVER = "build_server"

IMAGE_MODE_SCRIPT = "script"
IMAGE_MODE_PARTITION = "partition"
_VALID_IMAGE_MODES = (IMAGE_MODE_SCRIPT, IMAGE_MODE_PARTITION)

_REPORT_STATUSES = (STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED)
_TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)

# 同 lease 只能有一个排队/执行中/等待 Agent 的任务
_ACTIVE_JOB_STATES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_WAITING_AGENT)

_VERSION_STATE_OK = "OK"
_VERSION_STATE_UNKNOWN = "UNKNOWN_VERSION"
_VERSION_STATE_INCOMPATIBLE = "INCOMPATIBLE"
_VERSION_STATE_UPGRADE = "UPGRADE_REQUIRED"
_VERSION_STATE_PATCH_BEHIND = "PATCH_BEHIND"

# 目录列表形如：<a href="236/">236/</a>   07-Nov-2026 19:01       -
_LISTING_RE = re.compile(
    r'<a href="(\d+)/">[^<]*</a>\s*'
    r"(\d{2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2})"
)
# V1.00.01.236 / 1.0.0 / 1.2.3.4（第 4 段是 build，不参与 semver 门槛）。
# 必须至少 major.minor，纯 build 号（如 "154"）不算 semver，按 UNKNOWN_VERSION 处理。
_SEMVER_RE = re.compile(r"^[Vv]?(\d+)\.(\d+)(?:\.(\d+))?(?:\.\d+)*$")


@dataclass(frozen=True)
class SemVer:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


def parse_semver(value: str | None) -> SemVer | None:
    """解析 XOS 固件版本；支持 V1.00.01.236 / 1.0.0，解析不了返回 None。"""
    if not value:
        return None
    normalized = value.strip()
    if normalized.startswith("/"):  # 极端脏数据，如 shell 报错路径
        return None
    m = _SEMVER_RE.match(normalized)
    if not m:
        return None
    return SemVer(
        major=int(m.group(1)),
        minor=int(m.group(2) or 0),
        patch=int(m.group(3) or 0),
    )


def evaluate_version(
    current_version: str | None,
    required_major: int = 0,
    required_minor: int = 0,
    required_patch: int = 0,
) -> dict:
    """按 semver 给出门禁结论（res_apply / res_firmware_status 共用同一口径）。"""
    current = parse_semver(current_version)
    if current is None:
        return {
            "state": _VERSION_STATE_UNKNOWN,
            "upgrade_needed": False,
            "reason": "设备固件版本未知（探测未读到），无法做版本门禁",
            "recommendation": "可正常分配；建议刷一次固件或等巡检回填版本",
        }
    if required_major and current.major < required_major:
        return {
            "state": _VERSION_STATE_INCOMPATIBLE,
            "upgrade_needed": True,
            "reason": f"主版本 {current.major} < 要求 {required_major}，与当前服务端不兼容",
            "recommendation": f"必须升级到 {required_major}.x.y 及以上（major 变化不兼容）",
        }
    if required_minor and current.minor < required_minor:
        return {
            "state": _VERSION_STATE_UPGRADE,
            "upgrade_needed": True,
            "reason": f"次版本 {current.minor} < 要求 {required_minor}，必须升级",
            "recommendation": f"调 res_flash 升级到 {required_major or current.major}.{required_minor}.x 及以上",
        }
    if required_patch and current.patch < required_patch:
        return {
            "state": _VERSION_STATE_PATCH_BEHIND,
            "upgrade_needed": False,
            "reason": f"补丁版本 {current.patch} < 建议 {required_patch}（不强制）",
            "recommendation": f"可继续使用；建议空闲时升级到 {current.major}.{current.minor}.{required_patch}",
        }
    return {
        "state": _VERSION_STATE_OK,
        "upgrade_needed": False,
        "reason": "版本满足要求",
        "recommendation": "",
    }


# ---------- 版本目录（HTTP） ----------


def _http_get(url: str, timeout: int | None = None) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "quamm-tres/0.14"})
    with urllib.request.urlopen(req, timeout=timeout or settings.flash_http_timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _parse_listing(html: str, base_url: str) -> list[dict]:
    """解析 Apache 目录列表 -> [{version, url, build_time}]。"""
    versions: list[dict] = []
    for version, build_time in _LISTING_RE.findall(html):
        versions.append(
            {
                "version": version,
                "url": f"{base_url}/{version}/",
                "build_time": build_time,
            }
        )
    return versions


def _sort_versions(versions: list[dict]) -> None:
    def key(item: dict):
        try:
            ts = datetime.strptime(item["build_time"], "%d-%b-%Y %H:%M").timestamp()
        except (ValueError, KeyError):
            ts = 0.0
        return (ts, int(item["version"]))

    versions.sort(key=key, reverse=True)


def _require_project(product: ProductType) -> str:
    """MCP 工具口径：必须显式配置 db_project_name（避免误刷同名的非固件工程目录）。"""
    project = (product.db_project_name or "").strip()
    if not project:
        raise DomainError(
            "PRODUCT_NO_DB_PROJECT",
            f"机型 {product.type_code} 未配置 db_project_name，无法定位 DB 工程",
        )
    return project


def _fetch_versions(project: str) -> list[dict]:
    url = f"{settings.xos_base_url.rstrip('/')}/{project}/"
    try:
        html = _http_get(url)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise DomainError(
                "PRODUCT_NO_DB_PROJECT",
                f"DB 工程目录不存在：{project}",
                {"url": url},
            ) from e
        raise DomainError(
            "SOURCE_UNAVAILABLE", f"DB 服务器返回 HTTP {e.code}", {"url": url}
        ) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise DomainError(
            "SOURCE_UNAVAILABLE", f"DB 服务器不可达：{e}", {"url": url}
        ) from e
    versions = _parse_listing(html, url.rstrip("/"))
    _sort_versions(versions)
    return versions


def list_versions(session: Session, product_type: str, limit: int = 20) -> dict:
    product = session.get(ProductType, product_type)
    if product is None:
        raise DomainError("UNKNOWN_TARGET", f"机型 {product_type} 不存在于机型字典")
    project = _require_project(product)
    versions = _fetch_versions(project)
    return {
        "product_type": product_type,
        "db_project_name": project,
        "count": len(versions),
        "versions": versions[: max(1, int(limit))],
    }


def latest_version(session: Session, product_type: str) -> str:
    """取该机型最新版本号；供 res_flash(version=None) 与刷机线程共用。"""
    product = session.get(ProductType, product_type)
    if product is None:
        raise DomainError("UNKNOWN_TARGET", f"机型 {product_type} 不存在于机型字典")
    versions = _fetch_versions(_require_project(product))
    if not versions:
        raise DomainError(
            "NO_FIRMWARE_VERSION",
            f"工程 {_require_project(product)} 下没有可用版本目录",
        )
    return versions[0]["version"]


# ---------- 刷机任务 ----------


def _log_dir() -> Path:
    path = Path(settings.flash_log_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _work_dir() -> Path:
    """刷机根目录（logs/ 与 jobs/ 的父路径）；单 job 请用 `_job_work_dir`。"""
    path = Path(settings.flash_work_dir).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _job_work_dir(job_id: str) -> Path:
    """每个刷机 job 独立 cwd，隔离 downloads/tmp/test_report，避免并发互删。"""
    path = Path(settings.flash_work_dir).resolve() / "jobs" / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_command(project: str, ip: str, version: str, preserve_data: bool, log_path: str) -> list[str]:
    # -e data 排除 data 分区（保留用户数据）。erase 模式去掉该参数，其余不变。
    update_args = "-f -a -e data" if preserve_data else "-f -a"
    return [
        settings.python_bin,
        settings.dbxostest_path,
        "-t",
        project,
        "-i",
        ip,
        "-v",
        str(version),
        # argparse 会把以 '-' 开头的独立值当成选项，必须用 --opt=value 形式透传
        f"--update-args={update_args}",
        "-f",
        "--log",
        log_path,
    ]


def _execute(cmd: list[str], log_path: str, timeout: int, cwd: Path) -> tuple[int, str]:
    """跑 dbxostest；stdout/stderr 兜底写日志（正常时 dbxostest 自己写 --log）。

    cwd 必须是该 job 的独立工作目录（见 `_job_work_dir`），不可再共用全局 flash_work_dir。
    """
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd),
        )
    except subprocess.TimeoutExpired:
        return 124, f"刷机超时（>{timeout}s）"
    except (FileNotFoundError, OSError) as e:
        return 127, f"无法启动 dbxostest：{e}"

    output = (proc.stdout or "") + (proc.stderr or "")
    log_file = Path(log_path)
    try:
        if not log_file.exists() or log_file.stat().st_size == 0:
            log_file.write_text(output, encoding="utf-8")
    except OSError:
        logger.warning("write flash log failed: %s", log_path)
    return proc.returncode, output


def _tail_error(output: str) -> str:
    lines = [l for l in (output or "").splitlines() if l.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-3:])[:2000]


def _read_flashed_version(ip: str, port: int) -> str | None:
    """刷完回读设备固件版本；设备重启期间连不上则重试（最多约 50s）。"""
    for attempt in range(6):
        version = read_firmware_version(ip, port)
        if version:
            return version
        if attempt < 5:
            time.sleep(10)
    return None


def _run_job(job_id: str) -> None:
    """后台线程主流程：QUEUED -> RUNNING -> SUCCEEDED / FAILED。

    每个 job 使用独立工作目录（jobs/<job_id>/），结束后整目录删除；
    日志在 var/flash/logs/<job_id>.log，不随工作目录清理。
    """
    job_cwd: Path | None = None
    session = SessionLocal()
    try:
        job = session.get(FlashJob, job_id)
        if job is None:
            logger.error("flash job %s disappeared", job_id)
            return

        job_cwd = _job_work_dir(job_id)

        job.status = STATUS_RUNNING
        job.started_at = utcnow()
        session.commit()

        device = session.get(Device, job.device_id)
        product = session.get(ProductType, job.product_type) if job.product_type else None
        if device is None or not device.ip or product is None:
            raise DomainError(
                "DEVICE_INVALID",
                f"设备 {job.device_id} 缺少 IP 或机型信息，无法刷机",
            )

        if not job.version:
            job.version = latest_version(session, product.type_code)
            session.commit()

        cmd = _build_command(
            _require_project(product),
            device.ip,
            job.version,
            job.preserve_data,
            job.log_path or "",
        )
        logger.info("flash job %s start cwd=%s: %s", job_id, job_cwd, " ".join(cmd))
        rc, output = _execute(
            cmd, job.log_path or "", settings.flash_timeout_seconds, cwd=job_cwd
        )

        if rc == 0:
            flashed = _read_flashed_version(device.ip, device.adb_port or 5555)
            # 回读失败（设备重启未就绪）时退化为刚刷的目录版本号
            device.firmware_version = flashed or str(job.version)
            job.status = STATUS_SUCCEEDED
            job.error = None
        else:
            job.status = STATUS_FAILED
            job.error = _tail_error(output) or f"dbxostest 退出码 {rc}"
        job.finished_at = utcnow()
        session.commit()
        logger.info("flash job %s finished: %s", job_id, job.status)
    except DomainError as e:
        _fail_job(session, job_id, f"{e.code}: {e.message}")
    except Exception as e:  # noqa: BLE001
        logger.exception("flash job %s crashed", job_id)
        _fail_job(session, job_id, f"{type(e).__name__}: {e}")
    finally:
        session.close()
        if job_cwd is not None:
            _cleanup_job_workdir(job_id, job_cwd)


def _cleanup_job_workdir(job_id: str, job_cwd: Path) -> None:
    """删除 job 工作目录；失败只记 warning，不影响任务终态。不碰 logs/。"""
    try:
        if job_cwd.exists():
            shutil.rmtree(job_cwd)
            logger.info("flash job %s: removed workdir %s", job_id, job_cwd)
    except OSError as e:
        logger.warning("flash job %s: cleanup workdir %s failed: %s", job_id, job_cwd, e)


def _fail_job(session: Session, job_id: str, error: str) -> None:
    try:
        session.rollback()
        job = session.get(FlashJob, job_id)
        if job is not None:
            job.status = STATUS_FAILED
            job.error = error[:2000]
            job.finished_at = utcnow()
            session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("mark flash job %s failed", job_id)


def _image_meta_dict(
    *,
    source: str,
    image_mode: str | None,
    script_ini: str | None,
    partition: str | None,
    file_path: str | None,
    image_name: str | None,
    image_sha256: str | None,
) -> dict:
    return {
        "source": source,
        "mode": image_mode,
        "name": image_name,
        "sha256": image_sha256,
        "script_ini": script_ini,
        "partition": partition,
        "file_path": file_path,
    }


def _validate_image_args(
    image_mode: str | None,
    script_ini: str | None,
    partition: str | None,
    file_path: str | None,
    *,
    require_absolute: bool,
) -> str:
    """校验 image_mode 与路径参数；返回规范化后的 image_mode。"""
    mode = (image_mode or "").strip().lower() or None
    if mode not in _VALID_IMAGE_MODES:
        raise DomainError(
            "NEED_IMAGE",
            "需要 image_mode=script|partition，并提供对应镜像路径参数",
            {"image_mode": image_mode},
        )
    if mode == IMAGE_MODE_SCRIPT:
        if not (script_ini or "").strip():
            raise DomainError(
                "NEED_IMAGE",
                "image_mode=script 时必须提供 script_ini（qmimages/script.ini 绝对路径）",
            )
        script_ini = script_ini.strip()
        if require_absolute and not os.path.isabs(script_ini):
            raise DomainError(
                "IMAGE_PATH_OUTSIDE_WORK_ROOT",
                f"script_ini 必须是绝对路径：{script_ini}",
            )
    else:
        if not (partition or "").strip() or not (file_path or "").strip():
            raise DomainError(
                "NEED_IMAGE",
                "image_mode=partition 时必须提供 partition 与 file_path",
            )
        file_path = file_path.strip()
        if require_absolute and not os.path.isabs(file_path):
            raise DomainError(
                "IMAGE_PATH_OUTSIDE_WORK_ROOT",
                f"file_path 必须是绝对路径：{file_path}",
            )
    return mode


def build_xosadb2update_argv(
    *,
    device_ip: str,
    preserve_data: bool,
    image_mode: str,
    script_ini: str | None = None,
    partition: str | None = None,
    file_path: str | None = None,
) -> list[str]:
    """构造 xosadb2update 参数列表（不含 shell）；preserve_data 时带 -e data。"""
    argv = ["xosadb2update", "-f", "-a"]
    if preserve_data:
        argv.extend(["-e", "data"])
    argv.extend(["-i", device_ip])
    if image_mode == IMAGE_MODE_SCRIPT:
        argv.append(script_ini or "")
    else:
        argv.extend(["-p", partition or "", file_path or ""])
    return argv


def _command_string(argv: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in argv)


def _job_mode(job: FlashJob) -> str:
    executor = (job.executor or EXECUTOR_SERVER).strip()
    if executor == EXECUTOR_AGENT:
        return MODE_AGENT_LOCAL
    if executor == EXECUTOR_BUILD_SERVER:
        return MODE_BUILD_SERVER
    return MODE_SERVER_DB


def _runner_public(bs: BuildServer) -> dict:
    """编译机摘要：绝不包含 secret / secret_encrypted / auth 细节。"""
    return {
        "server_id": bs.server_id,
        "name": bs.name,
        "host": bs.host,
        "username": bs.username,
        "work_root": bs.work_root,
    }


def _resolve_build_server(
    session: Session, auth: AuthContext, build_server_id: str | None
) -> BuildServer:
    if build_server_id:
        bs = session.get(BuildServer, build_server_id)
        if bs is None:
            raise DomainError(
                "BUILD_SERVER_NOT_FOUND", f"编译服务器不存在：{build_server_id}"
            )
        if bs.principal_id != auth.principal.id and not auth.is_admin:
            raise DomainError("FORBIDDEN", "无权使用他人的编译服务器")
        return bs
    bs = (
        session.query(BuildServer)
        .filter(BuildServer.principal_id == auth.principal.id)
        .first()
    )
    if bs is None:
        raise DomainError(
            "BUILD_SERVER_NOT_FOUND",
            "当前账号未登记编译服务器；请先在设置页配置，或显式传 build_server_id",
        )
    return bs


def _remote_preflight_image(
    bs: BuildServer,
    secret: str | None,
    paths: list[str],
) -> None:
    """远端预检：xosadb2update 存在；路径绝对且 realpath 落在 ~/work_root 内；文件存在。"""
    from app.remote_ssh import run_remote

    work_root = (bs.work_root or "tres-auto").strip().lstrip("./")
    work_q = shlex.quote(work_root)
    script_parts = [
        "set -e",
        'if ! command -v xosadb2update >/dev/null 2>&1; then echo __TOOL_MISSING__; exit 2; fi',
        f'WORK=$(realpath "$HOME"/{work_q} 2>/dev/null) || {{ echo __WORK_MISSING__; exit 5; }}',
    ]
    for path in paths:
        pq = shlex.quote(path)
        script_parts.append(
            f'if [ ! -e {pq} ]; then echo __NOT_FOUND__={pq}; exit 3; fi; '
            f'REAL=$(realpath {pq}); '
            f'case "$REAL" in "$WORK"|"$WORK"/*) ;; *) echo __OUTSIDE__=$REAL; exit 4 ;; esac'
        )
    script_parts.append("echo __PREFLIGHT_OK__")
    remote_cmd = " ; ".join(script_parts)
    result = run_remote(
        bs.host,
        bs.port,
        bs.username,
        bs.auth_mode,
        secret,
        remote_cmd,
        timeout=15,
    )
    out = (result.stdout or "") + "\n" + (result.stderr or "")
    if "__TOOL_MISSING__" in out or result.rc == 2:
        raise DomainError(
            "RUNNER_TOOL_MISSING",
            f"编译机 {bs.host} 未安装 xosadb2update（command -v 失败）。"
            f"193 等环境需自行安装后再刷；TRES 不会自动安装。",
            {"host": bs.host, "server_id": bs.server_id},
        )
    if "__NOT_FOUND__" in out or result.rc == 3:
        raise DomainError(
            "IMAGE_NOT_FOUND",
            f"编译机上镜像路径不存在：{paths}",
            {"paths": paths, "host": bs.host},
        )
    if "__OUTSIDE__" in out or result.rc == 4:
        raise DomainError(
            "IMAGE_PATH_OUTSIDE_WORK_ROOT",
            f"镜像 realpath 必须落在 ~/{work_root} 内：{paths}",
            {"paths": paths, "work_root": work_root, "host": bs.host},
        )
    if "__WORK_MISSING__" in out or result.rc == 5:
        raise DomainError(
            "IMAGE_PATH_OUTSIDE_WORK_ROOT",
            f"编译机工作目录 ~/{work_root} 不存在",
            {"work_root": work_root, "host": bs.host},
        )
    if not result.ok or "__PREFLIGHT_OK__" not in out:
        raise DomainError(
            "RUNNER_PREFLIGHT_FAILED",
            f"编译机预检失败（rc={result.rc}）：{(result.stderr or result.stdout or '')[:500]}",
            {"host": bs.host, "server_id": bs.server_id},
        )


def _append_log(log_path: str | None, text: str) -> None:
    if not log_path or not text:
        return
    try:
        path = Path(log_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")
    except OSError:
        logger.warning("append flash log failed: %s", log_path)


# Agent watchdog 轮询间隔（秒）；测试可 monkeypatch 调小，避免真等 timeout。
_AGENT_WATCHDOG_POLL_SECONDS = 45


def _agent_last_activity(job: FlashJob) -> datetime | None:
    """reported_at / started_at / created_at 中最新非空时间。"""
    stamps = [t for t in (job.reported_at, job.started_at, job.created_at) if t is not None]
    return max(stamps) if stamps else None


def _agent_watchdog(job_id: str, timeout: int) -> None:
    """source=local：周期性检查 WAITING_AGENT/RUNNING；无活动超过 timeout 则标 FAILED。

    仅处理 executor=agent；已终态立即退出；查询异常只记日志不中断轮询。
    """
    timeout = max(1, int(timeout))
    poll = max(1, min(int(_AGENT_WATCHDOG_POLL_SECONDS), timeout))
    while True:
        time.sleep(poll)
        session = SessionLocal()
        try:
            job = session.get(FlashJob, job_id)
            if job is None:
                return
            if (job.executor or EXECUTOR_SERVER) != EXECUTOR_AGENT:
                return
            if job.status in _TERMINAL_STATUSES:
                return
            if job.status not in (STATUS_WAITING_AGENT, STATUS_RUNNING):
                return
            last = _agent_last_activity(job)
            if last is None:
                continue
            now = utcnow()
            idle = (now - last).total_seconds()
            if idle > timeout:
                job.status = STATUS_FAILED
                job.error = f"Agent 超过 {timeout} 秒未上报"
                job.finished_at = now
                session.commit()
                logger.info(
                    "flash job %s agent stale timeout (%ss, idle=%.0fs)",
                    job_id,
                    timeout,
                    idle,
                )
                return
        except Exception:  # noqa: BLE001
            logger.exception("flash agent watchdog failed for %s", job_id)
        finally:
            session.close()


def _run_build_server_job(job_id: str) -> None:
    """后台：QUEUED -> RUNNING -> SUCCEEDED/FAILED；经 SSH 在编译机跑 xosadb2update。"""
    from app.remote_ssh import run_remote
    from app.secret_crypto import decrypt_secret

    session = SessionLocal()
    try:
        job = session.get(FlashJob, job_id)
        if job is None:
            logger.error("flash job %s disappeared", job_id)
            return
        job.status = STATUS_RUNNING
        job.started_at = utcnow()
        session.commit()

        device = session.get(Device, job.device_id)
        bs = session.get(BuildServer, job.runner_id) if job.runner_id else None
        meta = job.image_meta or {}
        if device is None or not device.ip or bs is None:
            raise DomainError("DEVICE_INVALID", "设备或编译服务器信息缺失，无法刷机")

        try:
            secret = decrypt_secret(bs.secret_encrypted)
        except ValueError as exc:
            raise DomainError("MASTER_KEY_MISMATCH", str(exc)) from exc

        argv = build_xosadb2update_argv(
            device_ip=device.ip,
            preserve_data=bool(job.preserve_data),
            image_mode=meta.get("mode") or IMAGE_MODE_SCRIPT,
            script_ini=meta.get("script_ini"),
            partition=meta.get("partition"),
            file_path=meta.get("file_path"),
        )
        remote_cmd = _command_string(argv)
        logger.info("flash job %s build_server start: %s", job_id, remote_cmd)
        result = run_remote(
            bs.host,
            bs.port,
            bs.username,
            bs.auth_mode,
            secret,
            remote_cmd,
            timeout=settings.flash_timeout_seconds,
        )
        blob = ""
        if result.stdout:
            blob += result.stdout + "\n"
        if result.stderr:
            blob += result.stderr + "\n"
        _append_log(job.log_path, blob)

        if result.ok:
            flashed = _read_flashed_version(device.ip, device.adb_port or 5555)
            if flashed:
                device.firmware_version = flashed
            elif meta.get("name"):
                device.firmware_version = str(meta["name"])
            job.status = STATUS_SUCCEEDED
            job.error = None
            job.progress = 100
        else:
            job.status = STATUS_FAILED
            job.error = (
                _tail_error(blob) or f"xosadb2update 退出码 {result.rc}"
            )[:2000]
        job.finished_at = utcnow()
        session.commit()
        logger.info("flash job %s finished: %s", job_id, job.status)
    except DomainError as e:
        _fail_job(session, job_id, f"{e.code}: {e.message}")
    except Exception as e:  # noqa: BLE001
        logger.exception("flash job %s crashed", job_id)
        _fail_job(session, job_id, f"{type(e).__name__}: {e}")
    finally:
        session.close()


def _assert_lease_flashable(
    session: Session,
    auth: AuthContext,
    lease_id: str,
    preserve_data: bool,
    confirm_erase_data: bool,
) -> tuple[Lease, Device]:
    lease = session.get(Lease, lease_id)
    if lease is None:
        raise DomainError("NOT_FOUND", f"lease {lease_id} not found")
    if lease.principal_id != auth.principal.id and not auth.is_admin:
        raise DomainError("FORBIDDEN", "只能刷自己租约下的设备")
    if lease.state != LeaseState.GRANTED:
        raise DomainError(
            "INVALID_STATE", f"租约状态 {lease.state} 不可刷机（需 GRANTED）"
        )
    if not preserve_data and not confirm_erase_data:
        raise DomainError(
            "NEED_CONFIRM_ERASE",
            "preserve_data=False 会清空 data 分区；请显式传 confirm_erase_data=True",
        )
    device = session.get(Device, lease.device_id)
    if device is None or not device.ip:
        raise DomainError("DEVICE_NO_ADDRESS", f"设备 {lease.device_id} 没有 IP，无法刷机")
    return lease, device


def _assert_no_active_job(session: Session, lease_id: str) -> None:
    running = (
        session.query(FlashJob)
        .filter(
            FlashJob.lease_id == lease_id,
            FlashJob.status.in_(_ACTIVE_JOB_STATES),
        )
        .first()
    )
    if running is not None:
        raise DomainError(
            "FLASH_IN_PROGRESS",
            f"该租约已有刷机任务 {running.job_id}（{running.status}）",
            {"job_id": running.job_id, "status": running.status},
        )


def start_flash(
    session: Session,
    auth: AuthContext,
    lease_id: str,
    version: str | None = None,
    preserve_data: bool = True,
    confirm_erase_data: bool = False,
    source: str = SOURCE_DB,
    build_server_id: str | None = None,
    image_mode: str | None = None,
    script_ini: str | None = None,
    partition: str | None = None,
    file_path: str | None = None,
    image_name: str | None = None,
    image_sha256: str | None = None,
) -> dict:
    src = (source or SOURCE_DB).strip().lower()
    if src not in _VALID_SOURCES:
        raise DomainError(
            "INVALID_SOURCE",
            f"source 仅支持 {'|'.join(_VALID_SOURCES)}，收到：{source}",
        )

    _lease, device = _assert_lease_flashable(
        session, auth, lease_id, preserve_data, confirm_erase_data
    )
    _assert_no_active_job(session, lease_id)

    if src == SOURCE_LOCAL:
        return _start_local(
            session,
            lease_id,
            device,
            preserve_data=preserve_data,
            image_mode=image_mode,
            script_ini=script_ini,
            partition=partition,
            file_path=file_path,
            image_name=image_name,
            image_sha256=image_sha256,
        )
    if src == SOURCE_BUILD_SERVER:
        return _start_build_server(
            session,
            auth,
            lease_id,
            device,
            preserve_data=preserve_data,
            build_server_id=build_server_id,
            image_mode=image_mode,
            script_ini=script_ini,
            partition=partition,
            file_path=file_path,
            image_name=image_name,
            image_sha256=image_sha256,
        )
    return _start_db(
        session,
        lease_id,
        device,
        version=version,
        preserve_data=preserve_data,
    )


def _start_db(
    session: Session,
    lease_id: str,
    device: Device,
    *,
    version: str | None,
    preserve_data: bool,
) -> dict:
    product = session.get(ProductType, device.model_class) if device.model_class else None
    if product is None:
        raise DomainError(
            "PRODUCT_NO_DB_PROJECT",
            f"设备 {device.device_id} 未关联机型，无法定位 DB 工程",
        )
    project = _require_project(product)

    job_id = new_id("fj_", 6)
    log_path = str(_log_dir() / f"{job_id}.log")
    job = FlashJob(
        job_id=job_id,
        lease_id=lease_id,
        device_id=device.device_id,
        product_type=product.type_code,
        version=str(version) if version is not None else None,
        preserve_data=bool(preserve_data),
        status=STATUS_QUEUED,
        log_path=log_path,
        created_at=utcnow(),
        executor=EXECUTOR_SERVER,
        image_meta=_image_meta_dict(
            source=SOURCE_DB,
            image_mode=None,
            script_ini=None,
            partition=None,
            file_path=None,
            image_name=None,
            image_sha256=None,
        ),
        progress=0,
    )
    session.add(job)
    session.commit()

    threading.Thread(
        target=_run_job, args=(job_id,), name=f"flash-{job_id}", daemon=True
    ).start()

    return {
        "job_id": job_id,
        "status": STATUS_QUEUED,
        "lease_id": lease_id,
        "adb_address": device.adb_address,
        "db_project_name": project,
        "preserve_data": bool(preserve_data),
        "mode": MODE_SERVER_DB,
        "executor": EXECUTOR_SERVER,
        "hint": "任务已提交后台执行（约 5-15 分钟）；调 res_flash_status(job_id) 轮询进度与日志",
    }


def _start_local(
    session: Session,
    lease_id: str,
    device: Device,
    *,
    preserve_data: bool,
    image_mode: str | None,
    script_ini: str | None,
    partition: str | None,
    file_path: str | None,
    image_name: str | None,
    image_sha256: str | None,
) -> dict:
    mode = _validate_image_args(
        image_mode,
        script_ini,
        partition,
        file_path,
        require_absolute=False,
    )
    script_ini_v = (script_ini or "").strip() or None
    partition_v = (partition or "").strip() or None
    file_path_v = (file_path or "").strip() or None
    meta = _image_meta_dict(
        source=SOURCE_LOCAL,
        image_mode=mode,
        script_ini=script_ini_v,
        partition=partition_v,
        file_path=file_path_v,
        image_name=image_name,
        image_sha256=image_sha256,
    )
    argv = build_xosadb2update_argv(
        device_ip=device.ip,
        preserve_data=bool(preserve_data),
        image_mode=mode,
        script_ini=script_ini_v,
        partition=partition_v,
        file_path=file_path_v,
    )
    job_id = new_id("fj_", 6)
    log_path = str(_log_dir() / f"{job_id}.log")
    job = FlashJob(
        job_id=job_id,
        lease_id=lease_id,
        device_id=device.device_id,
        product_type=device.model_class,
        version=str(image_name) if image_name else None,
        preserve_data=bool(preserve_data),
        status=STATUS_WAITING_AGENT,
        log_path=log_path,
        created_at=utcnow(),
        executor=EXECUTOR_AGENT,
        image_meta=meta,
        progress=0,
    )
    session.add(job)
    session.commit()

    timeout = int(settings.flash_agent_timeout_seconds)
    threading.Thread(
        target=_agent_watchdog,
        args=(job_id, timeout),
        name=f"flash-watch-{job_id}",
        daemon=True,
    ).start()

    return {
        "job_id": job_id,
        "status": STATUS_WAITING_AGENT,
        "lease_id": lease_id,
        "mode": MODE_AGENT_LOCAL,
        "executor": EXECUTOR_AGENT,
        "adb_address": device.adb_address,
        "device_ip": device.ip,
        "preserve_data": bool(preserve_data),
        "image": meta,
        "command_argv": argv,
        "command": _command_string(argv),
        "report_hint": (
            f"在本机执行 command_argv 后调用 res_flash_report(job_id={job_id!r}, "
            "status='SUCCEEDED'|'FAILED'|'RUNNING', progress=..., log_tail=..., error=...)"
        ),
        "agent_timeout_seconds": timeout,
    }


def _start_build_server(
    session: Session,
    auth: AuthContext,
    lease_id: str,
    device: Device,
    *,
    preserve_data: bool,
    build_server_id: str | None,
    image_mode: str | None,
    script_ini: str | None,
    partition: str | None,
    file_path: str | None,
    image_name: str | None,
    image_sha256: str | None,
) -> dict:
    from app.secret_crypto import decrypt_secret

    mode = _validate_image_args(
        image_mode,
        script_ini,
        partition,
        file_path,
        require_absolute=True,
    )
    script_ini_v = (script_ini or "").strip() or None
    partition_v = (partition or "").strip() or None
    file_path_v = (file_path or "").strip() or None
    paths = [script_ini_v] if mode == IMAGE_MODE_SCRIPT else [file_path_v]
    paths = [p for p in paths if p]

    bs = _resolve_build_server(session, auth, build_server_id)
    try:
        secret = decrypt_secret(bs.secret_encrypted)
    except ValueError as exc:
        raise DomainError("MASTER_KEY_MISMATCH", str(exc)) from exc

    _remote_preflight_image(bs, secret, paths)

    meta = _image_meta_dict(
        source=SOURCE_BUILD_SERVER,
        image_mode=mode,
        script_ini=script_ini_v,
        partition=partition_v,
        file_path=file_path_v,
        image_name=image_name,
        image_sha256=image_sha256,
    )
    argv = build_xosadb2update_argv(
        device_ip=device.ip,
        preserve_data=bool(preserve_data),
        image_mode=mode,
        script_ini=script_ini_v,
        partition=partition_v,
        file_path=file_path_v,
    )
    job_id = new_id("fj_", 6)
    log_path = str(_log_dir() / f"{job_id}.log")
    job = FlashJob(
        job_id=job_id,
        lease_id=lease_id,
        device_id=device.device_id,
        product_type=device.model_class,
        version=str(image_name) if image_name else None,
        preserve_data=bool(preserve_data),
        status=STATUS_QUEUED,
        log_path=log_path,
        created_at=utcnow(),
        executor=EXECUTOR_BUILD_SERVER,
        image_meta=meta,
        runner_id=bs.server_id,
        progress=0,
    )
    session.add(job)
    session.commit()

    threading.Thread(
        target=_run_build_server_job,
        args=(job_id,),
        name=f"flash-bs-{job_id}",
        daemon=True,
    ).start()

    return {
        "job_id": job_id,
        "status": STATUS_QUEUED,
        "lease_id": lease_id,
        "mode": MODE_BUILD_SERVER,
        "executor": EXECUTOR_BUILD_SERVER,
        "adb_address": device.adb_address,
        "device_ip": device.ip,
        "preserve_data": bool(preserve_data),
        "runner": _runner_public(bs),
        "image": meta,
        "command_argv": argv,
        "command": _command_string(argv),
        "hint": "任务已在编译机后台执行；调 res_flash_status(job_id) 轮询进度与日志",
    }


def flash_report(
    session: Session,
    auth: AuthContext,
    job_id: str,
    status: str,
    progress: int | None = None,
    log_tail: str | None = None,
    error: str | None = None,
) -> dict:
    """Agent 上报本地刷机进度；仅 WAITING_AGENT/RUNNING 的 agent job 可推进。"""
    new_status = (status or "").strip().upper()
    if new_status not in _REPORT_STATUSES:
        raise DomainError(
            "INVALID_STATUS",
            f"status 只允许 {'/'.join(_REPORT_STATUSES)}，收到：{status}",
        )

    job = session.get(FlashJob, job_id)
    if job is None:
        raise DomainError("NOT_FOUND", f"flash job {job_id} not found")
    lease = session.get(Lease, job.lease_id)
    if lease is None or (lease.principal_id != auth.principal.id and not auth.is_admin):
        raise DomainError("FORBIDDEN", "只能上报自己租约的刷机任务")
    if (job.executor or EXECUTOR_SERVER) != EXECUTOR_AGENT:
        raise DomainError(
            "INVALID_STATE",
            f"job {job_id} 的 executor={job.executor} 不接受 Agent 上报",
        )

    if job.status in _TERMINAL_STATUSES:
        if job.status == new_status:
            return flash_status(session, auth, job_id)
        raise DomainError(
            "JOB_ALREADY_FINISHED",
            f"job {job_id} 已终态 {job.status}，不能再改为 {new_status}",
            {"job_id": job_id, "status": job.status, "requested": new_status},
        )

    if job.status not in (STATUS_WAITING_AGENT, STATUS_RUNNING):
        raise DomainError(
            "INVALID_STATE",
            f"job {job_id} 当前状态 {job.status} 不可上报",
        )

    now = utcnow()
    job.reported_at = now
    if progress is not None:
        try:
            job.progress = max(0, min(100, int(progress)))
        except (TypeError, ValueError):
            raise DomainError("VALIDATION_ERROR", f"progress 必须是整数：{progress}")
    if log_tail:
        _append_log(job.log_path, str(log_tail))
    if new_status == STATUS_RUNNING:
        job.status = STATUS_RUNNING
        if job.started_at is None:
            job.started_at = now
        job.error = None
    elif new_status == STATUS_SUCCEEDED:
        job.status = STATUS_SUCCEEDED
        if job.started_at is None:
            job.started_at = now
        job.finished_at = now
        job.error = None
        if job.progress < 100:
            job.progress = 100
    else:  # FAILED
        job.status = STATUS_FAILED
        if job.started_at is None:
            job.started_at = now
        job.finished_at = now
        job.error = (error or "Agent 上报失败")[:2000]

    session.commit()
    return flash_status(session, auth, job_id)


def _log_tail(log_path: str | None, lines: int = 50) -> list[str]:
    if not log_path:
        return []
    path = Path(log_path)
    try:
        if not path.is_file():
            return []
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return content.splitlines()[-lines:]


def flash_status(session: Session, auth: AuthContext, job_id: str) -> dict:
    job = session.get(FlashJob, job_id)
    if job is None:
        raise DomainError("NOT_FOUND", f"flash job {job_id} not found")
    lease = session.get(Lease, job.lease_id)
    if lease is not None and lease.principal_id != auth.principal.id and not auth.is_admin:
        raise DomainError("FORBIDDEN", "只能查询自己租约的刷机任务")

    return {
        "job_id": job.job_id,
        "status": job.status,
        "version": job.version,
        "device_id": job.device_id,
        "lease_id": job.lease_id,
        "preserve_data": job.preserve_data,
        "log_tail": _log_tail(job.log_path),
        "log_path": job.log_path,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "error": job.error,
        "mode": _job_mode(job),
        "executor": job.executor or EXECUTOR_SERVER,
        "runner_id": job.runner_id,
        "image": job.image_meta,
        "progress": int(job.progress or 0),
        "reported_at": job.reported_at.isoformat() if job.reported_at else None,
    }


def firmware_status(session: Session, device_id: str) -> dict:
    device = session.get(Device, device_id)
    if device is None:
        raise DomainError("NOT_FOUND", f"device {device_id} not found")

    product = session.get(ProductType, device.model_class) if device.model_class else None
    required_major = product.required_major if product else 0
    required_minor = product.required_minor if product else 0
    required_patch = product.required_patch if product else 0

    verdict = evaluate_version(
        device.firmware_version, required_major, required_minor, required_patch
    )
    return {
        "device_id": device.device_id,
        "product_type": device.model_class,
        "current_version": device.firmware_version,
        "required_major": required_major,
        "required_minor": required_minor,
        "required_patch": required_patch,
        "upgrade_needed": verdict["upgrade_needed"],
        "reason": verdict["reason"],
        "recommendation": verdict["recommendation"],
        "state": verdict["state"],
    }
