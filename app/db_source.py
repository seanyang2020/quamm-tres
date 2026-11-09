"""DB 构建源（吸纳 dbxostest.py 的核心逻辑，库式实现，经 MCP 释放）。

能力：
- 智能适配提供的 DB 目录：支持源别名（daily/release）、完整 URL、相对工程路径；
- 探测 DB 目录是否可用（HTTP 可达、目录类型：源根/工程/版本）；
- 枚举工程、发现整数 DB 版本（latest/ 目录、HTML 解析、二分兜底稀疏版本）；
- 列出/下载 CURRENT*.gz 包、解压并定位 qmimages/script.ini；
- 读取设备当前 DB 版本（adb shell /.DB_*）；
- 经 xosadb2update 执行 DB 版本升级。

只依赖标准库 + 系统 adb/xosadb2update，不引入 CLI/邮件/HTML 报告等外围逻辑。
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from app.domain.catalog import DomainError
from app.utils import utcnow

SOURCES = {
    "daily": "http://172.16.1.201/Daily/XOS/",
    "release": "http://172.16.1.201/release/XOS/",
}
DEFAULT_TIMEOUT = 8
MAX_VERSION = 2000
PACKAGE_RE = re.compile(r"CURRENT[^\s]*\.tar\.gz$", re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]+)"')
WORK_ROOT = Path(__file__).resolve().parent.parent / "var" / "db_source"


# ---------------- 路径智能适配 ----------------

def normalize_source(spec: str | None = None, project_path: str | None = None) -> dict:
    """把用户给的 DB 目录规格归一为 {base_url, project_url}。

    接受：源别名 daily/release；完整 http(s) URL；缺省回退 daily。
    project_path 可为多级（如 XOS_SDK_Daily/10xv_lv），拼到 base 下。
    """
    spec = (spec or "daily").strip()
    if spec in SOURCES:
        base = SOURCES[spec]
    elif spec.startswith("http://") or spec.startswith("https://"):
        base = spec if spec.endswith("/") else spec + "/"
    else:
        raise DomainError("INVALID_SOURCE", f"无法识别的 DB 源：{spec}（用 daily/release 或完整 URL）")

    project_url = None
    p = (project_path or "").strip("/")
    if p:
        project_url = base + p + "/"
    return {"source": spec, "base_url": base, "project_url": project_url}


# ---------------- HTTP 基础 ----------------

def _http(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        raise DomainError("SOURCE_UNREACHABLE", f"无法访问 {url}：{e}")


def url_ok(url: str, timeout: int = DEFAULT_TIMEOUT) -> bool:
    try:
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def _entries(url: str) -> list[dict]:
    html = _http(url)
    out = []
    for href in _HREF_RE.findall(html):
        if href in ("../", "./", "/") or href.startswith("?"):
            continue
        out.append({"name": href.rstrip("/"), "is_dir": href.endswith("/"), "href": href})
    return out


def list_projects(base_url: str) -> list[str]:
    return sorted({e["name"] for e in _entries(base_url) if e["is_dir"] and not e["name"].startswith(".")})


# ---------------- 可用性探测 ----------------

def _analyze(target: str) -> dict:
    entries = _entries(target)
    subdirs = [e["name"] for e in entries if e["is_dir"]]
    packages = [e["name"] for e in entries if not e["is_dir"] and PACKAGE_RE.search(e["name"])]
    numeric = sorted({int(s) for s in subdirs if s.isdigit()}, reverse=True)
    return {
        "subdirs": subdirs, "numeric_versions": numeric,
        "has_latest": "latest" in subdirs, "packages": packages,
    }


def probe_directory(spec: str | None = None, project_path: str | None = None) -> dict:
    """探测提供的 DB 目录是否可用，判断层级并给出可继续的入口。"""
    norm = normalize_source(spec, project_path)
    target = norm["project_url"] or norm["base_url"]
    if not url_ok(target):
        return {"usable": False, "target": target, "reason": "HTTP 不可达", **norm}

    info = _analyze(target)
    if info["packages"]:
        kind = "version"
    elif info["numeric_versions"] or info["has_latest"]:
        kind = "project"
    else:
        kind = "group"
    return {"usable": True, "target": target, "kind": kind, **info, **norm}


# ---------------- 版本发现 ----------------

def _binary_search_highest(project_url: str) -> int | None:
    left, right = 1, MAX_VERSION
    highest = None
    while left <= right:
        mid = (left + right) // 2
        if url_ok(project_url + f"{mid}/", timeout=4):
            highest = mid
            left = mid + 1
        else:
            right = mid - 1
    return highest


def find_versions(project_url: str, version: int | None = None) -> dict:
    """发现工程可用版本：指定版本优先，其次 latest/，再 HTML 解析，最后二分兜底。"""
    project_url = project_url if project_url.endswith("/") else project_url + "/"
    if version is not None:
        u = project_url + f"{version}/"
        if not url_ok(u):
            raise DomainError("VERSION_NOT_FOUND", f"指定版本 {version} 不存在：{u}")
        return {"requested": version, "selected": version, "version_url": u, "strategy": "specified"}

    latest_u = project_url + "latest/"
    if url_ok(latest_u):
        return {"selected": "latest", "version_url": latest_u, "strategy": "latest"}

    probe = _analyze(project_url)
    if probe["numeric_versions"]:
        top = probe["numeric_versions"][0]
        return {"selected": top, "version_url": project_url + f"{top}/",
                "all_versions": probe["numeric_versions"], "strategy": "listing"}

    top = _binary_search_highest(project_url)
    if top is None:
        raise DomainError("NO_VERSION", f"未找到任何可用版本：{project_url}")
    return {"selected": top, "version_url": project_url + f"{top}/", "strategy": "binary_search"}


def list_packages(version_url: str) -> list[dict]:
    version_url = version_url if version_url.endswith("/") else version_url + "/"
    out = []
    for e in _entries(version_url):
        if not e["is_dir"] and PACKAGE_RE.search(e["name"]):
            furl = version_url + e["href"]
            out.append({"name": e["name"], "url": furl})
    if not out:
        raise DomainError("NO_PACKAGE", f"版本目录无 CURRENT*.tar.gz：{version_url}")
    return out


# ---------------- 设备当前 DB 版本 ----------------

def _adb(addr: str, cmd: list[str], timeout: int = 8) -> tuple[int, str]:
    full = ["timeout", str(timeout), "adb", *cmd]
    try:
        p = subprocess.run(full, capture_output=True, text=True, timeout=timeout + 2)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "adb 超时"


def device_db_version(ip: str, port: int = 5555, project: str | None = None) -> dict:
    addr = f"{ip}:{port}"
    _adb(addr, ["connect", addr], timeout=6)
    rc, out = _adb(addr, ["-s", addr, "shell", "ls -1 /.DB_* 2>/dev/null"], timeout=8)
    files = [os.path.basename(x.strip()) for x in out.splitlines() if x.strip().startswith(".DB_")]
    found = []
    for f in files:
        m = re.match(r"\.DB_(.+?)_(\d+)(?:_(\d+))?$", f)
        if m:
            proj, ver, ts = m.group(1), int(m.group(2)), m.group(3)
            if project is None or project == proj or project.startswith(proj):
                found.append({"project": proj, "version": ver, "timestamp": ts, "file": f})
    if not found:
        return {"ip": ip, "version": None, "files": files}
    top = max(found, key=lambda x: x["version"])
    return {"ip": ip, "version": top["version"], "detail": top, "all": found}


# ---------------- 下载 / 解压 ----------------

def download_package(package_url: str, label: str) -> dict:
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    name = package_url.rsplit("/", 1)[-1]
    dest = WORK_ROOT / label.replace("/", "_") / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with urllib.request.urlopen(package_url, timeout=60) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        raise DomainError("DOWNLOAD_FAILED", f"下载失败 {package_url}：{e}")
    return {"package": str(dest), "name": name, "size": dest.stat().st_size}


def extract_and_find_ini(package_path: str, keep: bool = False) -> dict:
    tmp = tempfile.mkdtemp(prefix="dbxos_", dir=str(WORK_ROOT))
    try:
        with tarfile.open(package_path, "r:gz") as tf:
            tf.extractall(tmp)
    except (tarfile.TarError, OSError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise DomainError("EXTRACT_FAILED", f"解压失败：{e}")
    ini = None
    for pat in ("qmimages/script.ini", "*/qmimages/script.ini", "*/*/qmimages/script.ini"):
        hits = glob.glob(os.path.join(tmp, pat))
        if hits:
            ini = hits[0]
            break
    if ini is None:
        raise DomainError("NO_SCRIPT_INI", f"解压目录未找到 qmimages/script.ini：{tmp}")
    return {"extract_dir": tmp, "script_ini": ini, "keep": keep}


# ---------------- 升级 ----------------

def upgrade(ip: str, script_ini: str, update_args: str = "-f -a -e data",
            timeout: int = 600) -> dict:
    xosadb2update = shutil.which("xosadb2update") or "/home/scm/bin/xosadb2update"
    cmd = [xosadb2update, *update_args.split(), "-i", ip, script_ini]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DomainError("UPGRADE_TIMEOUT", "升级超时")
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        raise DomainError("UPGRADE_FAILED", f"升级失败（{p.returncode}）：{out[-500:]}")
    return {"ok": True, "ip": ip, "output_tail": out[-800:], "finished_at": utcnow().isoformat()}
