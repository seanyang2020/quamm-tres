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
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import cache_prune
from app.config import settings
from app.constants import LeaseState
from app.db import SessionLocal
from app.domain.catalog import DomainError
from app.domain.prober_real import read_firmware_version
from app.models import Device, FlashJob, Lease, ProductType
from app.security import AuthContext
from app.utils import new_id, utcnow

logger = logging.getLogger(__name__)

STATUS_QUEUED = "QUEUED"
STATUS_RUNNING = "RUNNING"
STATUS_SUCCEEDED = "SUCCEEDED"
STATUS_FAILED = "FAILED"

# 同 lease 只能有一个排队/执行中的任务
_ACTIVE_JOB_STATES = (STATUS_QUEUED, STATUS_RUNNING)

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
    path = Path(settings.flash_work_dir).resolve()
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


def _execute(cmd: list[str], log_path: str, timeout: int) -> tuple[int, str]:
    """跑 dbxostest；stdout/stderr 兜底写日志（正常时 dbxostest 自己写 --log）。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(_work_dir()),
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
    """后台线程主流程：QUEUED -> RUNNING -> SUCCEEDED / FAILED。"""
    # v0.28.3：记录刷机前的下载目录快照，结束后只删本次新增的镜像
    downloads_before = cache_prune.snapshot_downloads() if settings.flash_cache_prune_after_job else None
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
        logger.info("flash job %s start: %s", job_id, " ".join(cmd))
        rc, output = _execute(cmd, job.log_path or "", settings.flash_timeout_seconds)

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
        if downloads_before is not None:
            _prune_job_cache(job_id, downloads_before)


def _prune_job_cache(job_id: str, before: set[str]) -> None:
    """v0.28.3：刷机结束回收本次下载的镜像。

    只在没有其它 RUNNING job 时执行 —— 并发刷机时 downloads/ 里的新增文件可能属于
    另一个 job，误删会让对方中断。
    """
    try:
        session = SessionLocal()
        try:
            others = (
                session.query(FlashJob)
                .filter(FlashJob.status == STATUS_RUNNING, FlashJob.job_id != job_id)
                .count()
            )
        finally:
            session.close()
        if others:
            logger.info("flash job %s: skip cache prune (%s other job running)", job_id, others)
            return
        result = cache_prune.prune_flash_job_delta(before)
        if result["deleted_count"]:
            logger.info(
                "flash job %s: cache pruned %s items, freed %s",
                job_id, result["deleted_count"], result["freed_human"],
            )
    except Exception:  # noqa: BLE001
        logger.exception("flash job %s: cache prune failed", job_id)


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


def start_flash(
    session: Session,
    auth: AuthContext,
    lease_id: str,
    version: str | None = None,
    preserve_data: bool = True,
    confirm_erase_data: bool = False,
) -> dict:
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

    product = session.get(ProductType, device.model_class) if device.model_class else None
    if product is None:
        raise DomainError(
            "PRODUCT_NO_DB_PROJECT",
            f"设备 {device.device_id} 未关联机型，无法定位 DB 工程",
        )
    project = _require_project(product)

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
    )
    session.add(job)
    session.commit()

    threading.Thread(target=_run_job, args=(job_id,), name=f"flash-{job_id}", daemon=True).start()

    return {
        "job_id": job_id,
        "status": STATUS_QUEUED,
        "lease_id": lease_id,
        "adb_address": device.adb_address,
        "db_project_name": project,
        "preserve_data": bool(preserve_data),
        "hint": "任务已提交后台执行（约 5-15 分钟）；调 res_flash_status(job_id) 轮询进度与日志",
    }


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
