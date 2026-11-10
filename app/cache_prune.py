"""缓存回收（v0.28.3）：DB 源包/解压缓存 + 刷机下载/日志缓存。

为什么需要：
- `res_db_prepare` / `res_db_upgrade` 每次都把 CURRENT*.tar.gz 落到
  `var/db_source/<label>/`，再解压到 `var/db_source/dbxos_<rand>/`，两者都不回收，
  一次升级约 137MB×2；
- 刷机由 `dbxostest.py` 在 `var/flash` 里 cwd，镜像落在 `var/flash/downloads/`，
  刷完不再需要，历史上没人清。

策略（全部可配置，环境变量前缀 RES_）：
- `db_source_keep_days`        解压目录/包超过 N 天未改动即删（默认 2）
- `db_source_keep_per_label`   每个 label 目录只保留最新 N 个包（默认 1）
- `flash_cache_keep_days`      downloads/ 下超过 N 天未改动即删（默认 1）
- `flash_log_keep_days`        logs/ 与 test_report/ 保留天数（默认 30）
- `flash_cache_prune_after_job` 单个刷机 job 结束即删它本次下载的镜像（默认 True）

所有删除都是 best-effort：单个条目失败不中断整体清理，只记日志。
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

# 与 app/db_source.py 的 WORK_ROOT 保持一致（该模块按 __file__ 定位项目根）
WORK_ROOT = Path(__file__).resolve().parent.parent / "var" / "db_source"
EXTRACT_PREFIX = "dbxos_"


def _human(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _size_of(path: Path) -> int:
    """文件或目录的字节数（目录递归求和，忽略读失败的条目）。"""
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            continue
    return total


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _latest_mtime(path: Path) -> float:
    """目录自身与内部条目的最新 mtime —— 只要里面还有在被写的文件，就不算超期。"""
    latest = _mtime(path)
    try:
        for f in path.rglob("*"):
            try:
                latest = max(latest, f.stat().st_mtime)
            except OSError:
                continue
    except OSError:
        pass
    return latest


def _flash_root() -> Path:
    return Path(settings.flash_work_dir).resolve()


# ---------------- 统计 ----------------------------


def _summary(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "bytes": 0, "human": "0 B", "items": 0}
    try:
        items = len(list(path.iterdir()))
    except OSError:
        items = 0
    size = _size_of(path)
    return {"path": str(path), "bytes": size, "human": _human(size), "items": items}


def cache_stats() -> dict:
    """各类缓存的占用与条目数，供 API / CLI / 运维查看。"""
    db_root = WORK_ROOT
    flash_root = _flash_root()
    db_stat = _summary(db_root)
    flash_stat = _summary(flash_root)
    downloads = _summary(flash_root / "downloads")
    logs = _summary(flash_root / "logs")
    extract_dirs = []
    labels = []
    if db_root.exists():
        for d in db_root.iterdir():
            if not d.is_dir():
                continue
            size = _size_of(d)
            if d.name.startswith(EXTRACT_PREFIX):
                extract_dirs.append({"name": d.name, "bytes": size, "human": _human(size),
                                     "mtime_days": round((time.time() - _latest_mtime(d)) / 86400, 2)})
            else:
                pkgs = [f for f in d.glob("*") if f.is_file()]
                labels.append({"name": d.name, "packages": len(pkgs), "bytes": size,
                               "human": _human(size),
                               "mtime_days": round((time.time() - _mtime(d)) / 86400, 2)})
    return {
        "db_source": db_stat,
        "db_source_labels": labels,
        "db_source_extract_dirs": extract_dirs,
        "flash": flash_stat,
        "flash_downloads": downloads,
        "flash_logs": logs,
        "total_bytes": db_stat["bytes"] + flash_stat["bytes"] + logs["bytes"],
        "total_human": _human(db_stat["bytes"] + flash_stat["bytes"] + logs["bytes"]),
        "policy": {
            "db_source_keep_days": settings.db_source_keep_days,
            "db_source_keep_per_label": settings.db_source_keep_per_label,
            "flash_cache_keep_days": settings.flash_cache_keep_days,
            "flash_log_keep_days": settings.flash_log_keep_days,
            "flash_cache_prune_after_job": settings.flash_cache_prune_after_job,
        },
    }


# ---------------- DB 源缓存 ----------------------------


def prune_db_source(
    keep_days: int | None = None,
    keep_per_label: int | None = None,
    dry_run: bool = False,
) -> dict:
    """清 DB 源缓存：超期的解压目录、每个 label 超份数的旧包、超期的散落包。"""
    root = WORK_ROOT
    deleted: list[dict] = []
    freed = 0
    if not root.exists():
        return {"deleted": [], "freed_bytes": 0, "freed_human": "0 B", "dry_run": dry_run}

    keep_days = settings.db_source_keep_days if keep_days is None else keep_days
    keep_per_label = settings.db_source_keep_per_label if keep_per_label is None else keep_per_label
    cutoff = time.time() - keep_days * 86400

    # 1) 解压临时目录：按目录内最新 mtime 判断，超期才删
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir() or not d.name.startswith(EXTRACT_PREFIX):
            continue
        if _latest_mtime(d) >= cutoff:
            continue
        size = _size_of(d)
        if not dry_run:
            try:
                shutil.rmtree(d)
            except OSError as e:
                logger.warning("prune extract dir %s failed: %s", d, e)
                continue
        deleted.append({"path": str(d), "kind": "extract", "bytes": size})
        freed += size

    # 2) label 目录：只留最新 keep_per_label 个包，其余（含超期）删除
    for d in sorted(root.iterdir(), key=lambda p: p.name):
        if not d.is_dir() or d.name.startswith(EXTRACT_PREFIX):
            continue
        files = sorted((f for f in d.glob("*") if f.is_file()),
                       key=lambda f: _mtime(f), reverse=True)
        keep = max(0, int(keep_per_label))
        for f in files[keep:]:
            size = _size_of(f)
            if not dry_run:
                try:
                    f.unlink()
                except OSError as e:
                    logger.warning("prune package %s failed: %s", f, e)
                    continue
            deleted.append({"path": str(f), "kind": "package", "bytes": size})
            freed += size
        if not dry_run:
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    return {"deleted": deleted, "deleted_count": len(deleted),
            "freed_bytes": freed, "freed_human": _human(freed), "dry_run": dry_run}


# ---------------- 刷机缓存 ----------------------------


def prune_flash_cache(
    keep_days: int | None = None,
    log_keep_days: int | None = None,
    dry_run: bool = False,
) -> dict:
    """清刷机缓存：downloads 超期条目、tmp 全清、logs/test_report 超期。"""
    root = _flash_root()
    deleted: list[dict] = []
    freed = 0
    if not root.exists():
        return {"deleted": [], "freed_bytes": 0, "freed_human": "0 B", "dry_run": dry_run}

    keep_days = settings.flash_cache_keep_days if keep_days is None else keep_days
    log_keep_days = settings.flash_log_keep_days if log_keep_days is None else log_keep_days
    cutoff = time.time() - keep_days * 86400
    log_cutoff = time.time() - log_keep_days * 86400

    def _drop(path: Path, kind: str) -> None:
        nonlocal freed
        size = _size_of(path)
        if not dry_run:
            try:
                shutil.rmtree(path) if path.is_dir() else path.unlink()
            except OSError as e:
                logger.warning("prune %s %s failed: %s", kind, path, e)
                return
        deleted.append({"path": str(path), "kind": kind, "bytes": size})
        freed += size

    # 1) downloads：超期即删（镜像只在一次刷机里有意义）
    downloads = root / "downloads"
    if downloads.exists():
        for p in sorted(downloads.iterdir(), key=lambda x: x.name):
            if _mtime(p) < cutoff:
                _drop(p, "download")
        if not dry_run:
            try:
                if not any(downloads.iterdir()):
                    downloads.rmdir()
            except OSError:
                pass

    # 2) tmp：脚本的临时目录，直接清空
    tmp = root / "tmp"
    if tmp.exists():
        for p in sorted(tmp.iterdir(), key=lambda x: x.name):
            _drop(p, "tmp")

    # 3) logs / test_report：按保留天数
    for sub, kind in (("logs", "log"), ("test_report", "report")):
        d = root / sub
        if not d.exists():
            continue
        for p in sorted(d.iterdir(), key=lambda x: x.name):
            if _mtime(p) < log_cutoff:
                _drop(p, kind)

    return {"deleted": deleted, "deleted_count": len(deleted),
            "freed_bytes": freed, "freed_human": _human(freed), "dry_run": dry_run}


# ---------------- 单 job 增量清理 ----------------------------


def snapshot_downloads() -> set[str]:
    """记录 downloads/ 当前条目（相对路径），供 job 结束后比对增量。"""
    downloads = _flash_root() / "downloads"
    if not downloads.exists():
        return set()
    return {str(p.relative_to(downloads)) for p in downloads.rglob("*")}


def prune_flash_job_delta(before: set[str]) -> dict:
    """刷机结束：删掉本次 job 在 downloads/ 里新增的文件，并回收空目录。

    只在调用方确认没有其它 RUNNING job 时使用（并发会误删别人的镜像）。
    """
    downloads = _flash_root() / "downloads"
    if not downloads.exists():
        return {"deleted": [], "deleted_count": 0, "freed_bytes": 0, "freed_human": "0 B"}
    after = {str(p.relative_to(downloads)) for p in downloads.rglob("*")}
    new_items = after - before

    deleted: list[dict] = []
    freed = 0
    files = [dl for dl in sorted(new_items) if (downloads / dl).is_file()]
    for rel in files:
        p = downloads / rel
        size = _size_of(p)
        try:
            p.unlink()
        except OSError as e:
            logger.warning("prune job download %s failed: %s", p, e)
            continue
        deleted.append({"path": rel, "kind": "download", "bytes": size})
        freed += size

    # 自底向上删空目录
    dirs = [dl for dl in sorted(new_items) if (downloads / dl).is_dir()]
    for rel in sorted(dirs, key=lambda x: x.count("/") + x.count("\\"), reverse=True):
        p = downloads / rel
        try:
            if p.exists() and not any(p.iterdir()):
                p.rmdir()
        except OSError:
            pass
    try:
        if not any(downloads.iterdir()):
            downloads.rmdir()
    except OSError:
        pass

    return {"deleted": deleted, "deleted_count": len(deleted),
            "freed_bytes": freed, "freed_human": _human(freed)}


def prune_all(dry_run: bool = False) -> dict:
    """DB 源 + 刷机缓存一起清，worker 定时任务与 API「立即清理」共用。"""
    before = cache_stats()
    db = prune_db_source(dry_run=dry_run)
    flash = prune_flash_cache(dry_run=dry_run)
    after = cache_stats()
    return {
        "dry_run": dry_run,
        "db_source": db,
        "flash": flash,
        "freed_bytes": db["freed_bytes"] + flash["freed_bytes"],
        "freed_human": _human(db["freed_bytes"] + flash["freed_bytes"]),
        "before_total_human": before["total_human"],
        "after_total_human": after["total_human"],
    }
