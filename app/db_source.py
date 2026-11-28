"""DB 构建源（吸纳 dbxostest.py 的核心逻辑，库式实现，经 MCP 释放）。

能力：
- 智能适配提供的 DB 目录：支持源别名（daily/release）、完整 URL、局域网地址、相对工程路径；
- 探测 DB 目录是否可用（HTTP 可达、目录类型：源根/工程/版本）；
- 枚举工程、发现整数 DB 版本（latest/ 目录、HTML 解析、二分兜底稀疏版本）；
- 版本目录特征词提炼、按 feature 关键字（AND）过滤版本/包名；
- 列出/下载 CURRENT*.gz 包（同版本多包时优先 debug，否则最大包，对齐 dbxostest）；
- 解压并定位 qmimages/script.ini；
- 读取设备当前 DB 版本（adb shell /.DB_*）；
- 经 xosadb2update 执行 DB 版本升级。

只依赖标准库 + 系统 adb/xosadb2update，不引入 CLI/邮件/HTML 报告等外围逻辑。
"""
from __future__ import annotations

import glob
import json
import logging
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

logger = logging.getLogger(__name__)

SOURCES = {
    "daily": "http://172.16.1.201/Daily/XOS/",
    "release": "http://172.16.1.201/release/XOS/",
}
DEFAULT_TIMEOUT = 8
MAX_VERSION = 2000
PACKAGE_RE = re.compile(r"CURRENT[^\s]*\.tar\.gz$", re.IGNORECASE)
_HREF_RE = re.compile(r'href="([^"]+)"')
_IPV4_RE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})([/\\].*)?$")
_DATE_TOKEN_RE = re.compile(r"^(?:\d{8}|\d{4}-\d{2}-\d{2})$")
_FEATURE_SPLIT_RE = re.compile(r"[_\-\.\s/]+")
# Apache 列表行里偶发的字节大小（用于无 debug 时选最大包）
_SIZE_RE = re.compile(
    r'href="([^"]+\.tar\.gz)"[^<]*</a>\s+\S+\s+\S+\s+(\d+)',
    re.IGNORECASE,
)
WORK_ROOT = Path(__file__).resolve().parent.parent / "var" / "db_source"

# v0.23.0：xosadb2update 默认路径做成可配置常量（环境变量 XOSADB2UPDATE_PATH 可覆盖）。
# 不改变 upgrade() 的默认 update_args：仍是 -f -a -e data（保留 data 区，不升级 data 区）。
# v0.28.4：默认改为全局路径 /usr/local/bin/xosadb2update，整机只放一份，所有用户/服务共享。
XOSADB2UPDATE_PATH = "/usr/local/bin/xosadb2update"


# ---------------- 路径智能适配 ----------------

def _xos_base_url() -> str:
    try:
        from app.config import settings

        base = (settings.xos_base_url or SOURCES["daily"]).rstrip("/") + "/"
        return base
    except Exception:
        return SOURCES["daily"]


def _ensure_trailing_slash(url: str) -> str:
    return url if url.endswith("/") else url + "/"


def _lan_to_http(spec: str) -> str | None:
    """把局域网路径归一为 http://host/path/；无法识别返回 None。"""
    raw = (spec or "").strip()
    if not raw:
        return None
    # smb://host/path
    if raw.lower().startswith("smb://"):
        rest = raw[6:].lstrip("/")
        return _ensure_trailing_slash("http://" + rest.replace("\\", "/"))
    # \\host\path 或 //host/path
    if raw.startswith("\\\\") or raw.startswith("//"):
        rest = raw.lstrip("\\/").replace("\\", "/")
        return _ensure_trailing_slash("http://" + rest)
    # host\path（含反斜杠）
    if "\\" in raw:
        return _ensure_trailing_slash("http://" + raw.replace("\\", "/"))
    # 172.16.1.201/Daily/... 或 172.16.1.201
    m = _IPV4_RE.match(raw)
    if m:
        host, path = m.group(1), (m.group(2) or "").replace("\\", "/")
        return _ensure_trailing_slash("http://" + host + path)
    return None


def normalize_source(spec: str | None = None, project_path: str | None = None) -> dict:
    """把用户给的 DB 目录规格归一为 {base_url, project_url}。

    接受：源别名 daily/release；完整 http(s) URL；缺省回退 daily。
    project_path 可为多级（如 XOS_SDK_Daily/10xv_lv），拼到 base 下。
    """
    spec = (spec or "daily").strip()
    if spec in SOURCES:
        base = SOURCES[spec]
    elif spec.startswith("http://") or spec.startswith("https://"):
        base = _ensure_trailing_slash(spec)
    else:
        lan = _lan_to_http(spec)
        if lan:
            base = lan
        else:
            raise DomainError(
                "INVALID_SOURCE",
                f"无法识别的 DB 源：{spec}（用 daily/release 或完整 URL）",
            )

    project_url = None
    p = (project_path or "").strip("/")
    if p:
        project_url = base + p + "/"
    return {"source": spec, "base_url": base, "project_url": project_url}


def normalize_project_spec(spec: str | None) -> dict:
    """归一化机型 db_project_name：别名 / URL / 局域网 / 相对工程名。

    返回 {source, base_url, project_url, normalized}。
    - daily/release：当作源根（project_url=None，normalized=别名）
    - 完整 http(s) / 局域网地址：归一为 http URL（末尾 /）
    - 相对工程名（含多级）：拼到 xos_base_url 下
    """
    raw = (spec or "").strip()
    if not raw:
        raise DomainError("PRODUCT_NO_DB_PROJECT", "db_project_name 为空，无法定位 DB 工程")

    if raw in SOURCES:
        base = SOURCES[raw]
        return {
            "source": raw,
            "base_url": base,
            "project_url": None,
            "normalized": raw,
        }

    if raw.startswith("http://") or raw.startswith("https://"):
        url = _ensure_trailing_slash(raw)
        return {
            "source": url,
            "base_url": url,
            "project_url": url,
            "normalized": url,
        }

    lan = _lan_to_http(raw)
    if lan:
        return {
            "source": lan,
            "base_url": lan,
            "project_url": lan,
            "normalized": lan,
        }

    # 相对工程名 / 多级相对路径
    base = _xos_base_url()
    rel = raw.strip("/")
    project_url = base + rel + "/"
    return {
        "source": "daily",
        "base_url": base,
        "project_url": project_url,
        "normalized": rel,
    }


def parse_feature_filter(raw) -> list[str]:
    """解析 feature_filter（JSON 数组或逗号分隔）为非空关键字列表。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()]
    text = str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            data = None
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    return [p.strip() for p in re.split(r"[,;]+", text) if p.strip()]


def matches_features(name: str, features: list[str] | None) -> bool:
    """目录名/包名是否包含全部 feature 关键字（AND，大小写不敏感）。"""
    if not features:
        return True
    lower = (name or "").lower()
    return all(f.lower() in lower for f in features if f)


def _tokenize_name(name: str) -> list[str]:
    parts = [p for p in _FEATURE_SPLIT_RE.split(name or "") if p]
    out = []
    for p in parts:
        if p.isdigit():
            continue
        if _DATE_TOKEN_RE.match(p):
            continue
        out.append(p)
    return out


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


def list_version_features(project_url: str) -> dict:
    """列出工程下版本子目录并提炼特征词（剔除日期与纯数字版本 token）。

    返回 {features:[{name,count,dirs}], versions:[...]}。
    """
    project_url = _ensure_trailing_slash(project_url)
    entries = _entries(project_url)
    versions = sorted(
        {e["name"] for e in entries if e["is_dir"] and not e["name"].startswith(".")}
    )
    stats: dict[str, dict] = {}
    for name in versions:
        for tok in _tokenize_name(name):
            key = tok.lower()
            bucket = stats.setdefault(key, {"name": tok, "count": 0, "dirs": []})
            bucket["count"] += 1
            if name not in bucket["dirs"]:
                bucket["dirs"].append(name)
    features = sorted(stats.values(), key=lambda x: (-x["count"], x["name"].lower()))
    return {"features": features, "versions": versions}


def filter_version_dirs(
    dirs: list[str], features: list[str] | None = None
) -> list[str]:
    """按 feature 关键字（AND）过滤版本目录名。"""
    if not features:
        return list(dirs)
    return [d for d in dirs if matches_features(d, features)]


def select_best_package(packages: list[dict]) -> dict | None:
    """同版本多包时优先 debug，否则选体积最大（无 size 时退回首个）。

    规则对齐 dbxostest.select_best_package / 服务端列表选型。
    """
    if not packages:
        return None
    if len(packages) == 1:
        return packages[0]
    debug = [p for p in packages if "debug" in (p.get("name") or "").lower()]
    if debug:
        return debug[0]
    sized = [p for p in packages if isinstance(p.get("size"), int)]
    if sized:
        return max(sized, key=lambda p: int(p["size"]))
    return packages[0]


def list_packages(
    version_url: str,
    features: list[str] | None = None,
    *,
    select_best: bool = False,
) -> list[dict]:
    """列出版本目录下 CURRENT*.tar.gz；可按 feature 过滤；可选只返回最佳包。

    select_best=True 时返回单元素列表（debug 优先，否则最大包），对齐 dbxostest。
    """
    version_url = _ensure_trailing_slash(version_url)
    html = _http(version_url)
    sizes: dict[str, int] = {}
    for name, size_s in _SIZE_RE.findall(html):
        try:
            sizes[name.rstrip("/")] = int(size_s)
        except ValueError:
            pass

    out = []
    for href in _HREF_RE.findall(html):
        if href in ("../", "./", "/") or href.startswith("?") or href.endswith("/"):
            continue
        name = href.rstrip("/")
        if not PACKAGE_RE.search(name):
            continue
        if not matches_features(name, features):
            continue
        item = {"name": name, "url": version_url + href}
        if name in sizes:
            item["size"] = sizes[name]
        out.append(item)
    if not out:
        raise DomainError("NO_PACKAGE", f"版本目录无 CURRENT*.tar.gz：{version_url}")
    if select_best:
        best = select_best_package(out)
        return [best] if best else out
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
    xosadb2update = (
        os.environ.get("XOSADB2UPDATE_PATH")
        or shutil.which("xosadb2update")
        or XOSADB2UPDATE_PATH
    )
    cmd = [xosadb2update, *update_args.split(), "-i", ip, script_ini]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise DomainError("UPGRADE_TIMEOUT", "升级超时")
    out = (p.stdout or "") + (p.stderr or "")
    if p.returncode != 0:
        raise DomainError("UPGRADE_FAILED", f"升级失败（{p.returncode}）：{out[-500:]}")
    return {"ok": True, "ip": ip, "output_tail": out[-800:], "finished_at": utcnow().isoformat()}


# ---------------- 缓存回收（v0.28.3） ----------------

def discard_work(package_path: str | None, extract_dir: str | None) -> dict:
    """删掉本次下载的包与解压目录（端到端升级结束后调用，script.ini 已用完）。"""
    freed = 0
    removed: list[str] = []
    pkg = Path(package_path) if package_path else None
    if pkg is not None and pkg.exists():
        try:
            freed += pkg.stat().st_size
            pkg.unlink()
            removed.append(str(pkg))
        except OSError as e:
            logger.warning("discard package %s failed: %s", pkg, e)
    if pkg is not None:
        try:
            if pkg.parent.exists() and not any(pkg.parent.iterdir()):
                pkg.parent.rmdir()
        except OSError:
            pass
    ext = Path(extract_dir) if extract_dir else None
    if ext is not None and ext.exists():
        try:
            freed += sum(f.stat().st_size for f in ext.rglob("*") if f.is_file())
            shutil.rmtree(ext)
            removed.append(str(ext))
        except OSError as e:
            logger.warning("discard extract dir %s failed: %s", ext, e)
    return {"removed": removed, "freed_bytes": freed}


def prune_work_root(keep_days: int | None = None, keep_per_label: int | None = None) -> dict:
    """按保留策略清 var/db_source（下载前调用，避免越积越多）。"""
    from app import cache_prune

    result = cache_prune.prune_db_source(keep_days=keep_days, keep_per_label=keep_per_label)
    return {
        "deleted_count": int(result.get("deleted_count", 0) or 0),
        "freed_bytes": int(result.get("freed_bytes", 0) or 0),
    }
