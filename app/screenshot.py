"""设备截图 + 芯片/浮点 ABI 判定（v0.23.0）。

真实 XOS 是精简嵌入式 Linux，无 getprop；截图靠设备端静态 ARM 二进制读 framebuffer
生成 JPEG，再 adb pull 回主机。两个二进制按浮点 ABI 二选一（用错会在设备上报
illegal instruction）：

- ``vxosdbg``：硬浮点（hardfloat/eabihf，带 VFP 的芯片，如 qm102v / qm108h / qm10xv_full）
- ``xosdbg`` ：软浮点（softfloat/eabi，无 FPU，如 qm102）

浮点 ABI 三级判定：① ProductType 配置（float_abi/has_fpu）→ ② adb 读
/proc/cpuinfo 的 Features（含 vfp/neon 即硬浮点）→ ③ 运行时兜底：分别 push 两个
二进制执行 ``--version``，能正常返回版本的即为可运行二进制。

所有 adb 命令同时用 GNU ``timeout`` 与 subprocess 超时双重包裹，绝不无限挂起
（写法对齐 app/domain/prober_real.py）。注意本机 adb 不回传远端 shell 退出码，
故设备端命令统一追加 ``; echo __QUAMM_RC__=$?`` 由输出里解析真实 rc。

只依赖标准库 + subprocess；访问库（device_id -> ip/port + 机型浮点配置）与领域错误
均为函数内延迟导入，保持本模块顶层零第三方依赖。
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("quamm-tres.screenshot")

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
SOFT_BIN = "xosdbg"      # 软浮点
HARD_BIN = "vxosdbg"     # 硬浮点
SCREENSHOT_DIR = Path(__file__).resolve().parent / "var" / "screenshots"

ADB_TIMEOUT = 15                 # 单条 adb 命令 GNU timeout / subprocess 超时（秒）
REMOTE_DIRS = ("/data", "/tmp")  # /data 优先；不可写/空间不足自动退 /tmp

_RC_MARKER = "__QUAMM_RC__="
_OK_MARKER = "__QUAMM_OK__"
_RC_RE = re.compile(r"__QUAMM_RC__=(\d+)")
_FEATURES_RE = re.compile(r"^\s*features\s*:\s*(.+)$", re.I | re.M)
_SAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9_.-]")
_SAFE_IP_RE = re.compile(r"[A-Za-z0-9._-]+")
_SAFE_FB_RE = re.compile(r"/[A-Za-z0-9._/-]+")
_SAFE_FILENAME_RE = re.compile(r"[A-Za-z0-9_.-]+\.jpg")


def _fail(code: str, message: str):
    """延迟导入领域错误，供 MCP/REST 统一转 {"error":...}。"""
    from app.domain.catalog import DomainError

    raise DomainError(code, message)


# ---------------- adb 基础（GNU timeout + subprocess 双保险） ----------------

def _adb(cmd: list[str], timeout: int = ADB_TIMEOUT) -> tuple[int, str]:
    """跑一条 adb；返回 (adb 自身 returncode, stdout+stderr)。超时返回 124。"""
    full = ["timeout", str(timeout), "adb", *cmd]
    try:
        proc = subprocess.run(
            full, capture_output=True, text=True, timeout=timeout + 2, check=False
        )
    except subprocess.TimeoutExpired:
        return 124, "adb 超时"
    except (FileNotFoundError, OSError):
        return 127, ""
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _shell(addr: str, command: str, timeout: int = ADB_TIMEOUT) -> tuple[int, str]:
    """在设备执行 shell 命令；从输出里的 __QUAMM_RC__=N 解析远端真实退出码。

    本机 adb 不回传远端 rc（adb shell 恒返回 0），因此命令尾部追加 marker。
    返回 (远端 rc, 去掉 marker 行后的输出)；marker 缺失（adb 超时/断连）返回 -1。
    """
    rc, out = _adb(
        ["-s", addr, "shell", command + f"; echo {_RC_MARKER}$?"], timeout
    )
    matches = _RC_RE.findall(out)
    remote_rc = int(matches[-1]) if matches else -1
    cleaned = "\n".join(
        line for line in out.splitlines() if _RC_MARKER not in line
    ).strip()
    if rc == 124:
        return -1, cleaned
    return remote_rc, cleaned


def _push(addr: str, local: str, remote: str, timeout: int = ADB_TIMEOUT) -> bool:
    # 必须带 -s：多设备同时在线时无 -s 的 push 会报 "more than one device/emulator"。
    rc, out = _adb(["-s", addr, "push", local, remote], timeout)
    low = out.lower()
    return rc == 0 and "error" not in low and "failed to copy" not in low


def _pull(addr: str, remote: str, local: str, timeout: int = ADB_TIMEOUT) -> bool:
    rc, out = _adb(["-s", addr, "pull", remote, local], timeout)
    low = out.lower()
    return rc == 0 and "error" not in low and "does not exist" not in low


def _connect(addr: str, timeout: int = ADB_TIMEOUT) -> bool:
    _, out = _adb(["connect", addr], timeout)
    return "connected" in out.lower()


def _disconnect(addr: str, timeout: int = ADB_TIMEOUT) -> None:
    _adb(["disconnect", addr], timeout)


def _cleanup(addr: str, *paths: str, timeout: int = ADB_TIMEOUT) -> None:
    if not paths:
        return
    _shell(addr, "rm -f " + " ".join(paths), timeout)  # best-effort


# ---------------- 浮点 ABI 判定 ----------------

def _parse_features(cpuinfo: str) -> str | None:
    m = _FEATURES_RE.search(cpuinfo.replace("\r", "\n"))
    return " ".join(m.group(1).split()) if m else None


def _features_have_fpu(features: str) -> bool:
    tokens = features.lower().split()
    return any(t.startswith("vfp") or t == "neon" for t in tokens)


def _config_float_abi(product_config: dict | None) -> str | None:
    """① ProductType 配置：仅 hard/soft 显式值或 has_fpu=True 才视为已判定。"""
    cfg = product_config or {}
    fa = cfg.get("float_abi")
    if fa in ("hard", "soft"):
        return fa
    if cfg.get("has_fpu") is True:
        return "hard"
    return None


def _runtime_probe(addr: str, timeout: int = ADB_TIMEOUT) -> str:
    """③ 运行时兜底：分别 push 两个二进制执行 --version，挑可正常运行的那个。

    两个都能跑说明 CPU 带 VFP，按约定硬浮点才是正确 ABI（优先 vxosdbg）。
    """
    results: dict[str, bool] = {}
    for name in (HARD_BIN, SOFT_BIN):
        local = VENDOR_DIR / name
        remote = f"/tmp/quamm_probe_{name}"
        if not local.is_file() or not _push(addr, str(local), remote, timeout):
            results[name] = False
            _cleanup(addr, remote, timeout=timeout)
            continue
        rc, out = _shell(addr, f"chmod +x {remote}; {remote} --version 2>&1", timeout)
        low = out.lower()
        ok = rc == 0 and "version" in low and "illegal" not in low and "not found" not in low
        results[name] = ok
        _cleanup(addr, remote, timeout=timeout)
    if results.get(HARD_BIN):
        return "hard"   # 硬浮点可运行（含两者皆可）→ 带 FPU，硬浮点为正确 ABI
    if results.get(SOFT_BIN):
        return "soft"
    _fail("FLOAT_ABI_UNKNOWN", "运行时兜底也无法判定浮点 ABI（两个二进制均无法执行）")


def _resolve_float_abi(
    addr: str, product_config: dict | None, timeout: int = ADB_TIMEOUT
) -> tuple[str, str]:
    """返回 (float_abi: hard/soft, 判定来源)。"""
    abi = _config_float_abi(product_config)
    if abi:
        return abi, "product_type"
    _, cpuinfo = _shell(addr, "cat /proc/cpuinfo 2>/dev/null", timeout)
    features = _parse_features(cpuinfo)
    if features is not None:
        return ("hard" if _features_have_fpu(features) else "soft"), "cpuinfo"
    return _runtime_probe(addr, timeout), "runtime_probe"


# ---------------- 目标解析（延迟导入，避免顶层依赖 SQLAlchemy） ----------------

def _own_session_if_needed(session):
    """未传入 session 时惰性开一个会话：查库目标 + 写截图记录都需要会话。

    返回 (session, 需要调用方关闭的自有会话或 None)。
    """
    if session is not None:
        return session, None
    from app.db import SessionLocal

    s = SessionLocal()
    return s, s


def _resolve_target(device_id: str | None, ip: str | None, port: int | None, session):
    """返回 (ip, port, product_config)。device_id 优先从库读 ip/port 与机型浮点配置。"""
    if device_id:
        if session is None:
            _fail("DB_UNAVAILABLE", "按 device_id 截图需要数据库会话")
        from app.models import Device, ProductType

        dev = session.get(Device, device_id)
        if dev is None:
            _fail("DEVICE_NOT_FOUND", f"设备不存在：{device_id}")
        if not dev.ip:
            _fail("DEVICE_NO_IP", f"设备 {device_id} 未登记 IP")
        cfg: dict = {}
        if dev.model_class:
            pt = session.get(ProductType, dev.model_class)
            if pt is not None:
                cfg = {
                    "float_abi": getattr(pt, "float_abi", None),
                    "has_fpu": getattr(pt, "has_fpu", None),
                    "chip_model": getattr(pt, "chip_model", None),
                }
        return dev.ip, (dev.adb_port or 5555), cfg
    if not ip:
        _fail("MISSING_TARGET", "需提供 device_id 或 ip")
    return ip, (port or 5555), {}


def _validate(ip: str, port: int, quality: int, rotate: int, framebuffer: str) -> None:
    if not _SAFE_IP_RE.fullmatch(ip):
        _fail("INVALID_PARAM", f"非法 ip：{ip}")
    if not (1 <= int(port) <= 65535):
        _fail("INVALID_PARAM", f"非法 port：{port}")
    if not (1 <= int(quality) <= 100):
        _fail("INVALID_PARAM", "quality 须在 1-100")
    if int(rotate) not in (0, 90, 180, 270):
        _fail("INVALID_PARAM", "rotate 须为 0/90/180/270")
    if not _SAFE_FB_RE.fullmatch(framebuffer):
        _fail("INVALID_PARAM", f"非法 framebuffer：{framebuffer}")


# ---------------- 截图执行 ----------------

def _valid_jpeg(path: Path) -> bool:
    try:
        data = path.read_bytes()
    except OSError:
        return False
    return len(data) > 100 and data[:3] == b"\xff\xd8\xff" and data[-2:] == b"\xff\xd9"


def _capture_once(
    addr: str,
    remote_dir: str,
    bin_name: str,
    quality: int,
    rotate: int,
    framebuffer: str,
    token: str,
    local_path: Path,
    timeout: int,
) -> tuple[str, bool]:
    """在单个 (目录, 二进制) 组合上尝试一次完整截图；返回 (说明, 是否成功)。

    成功后 jpg 已 pull 到 local_path。无论成败都 best-effort 清理本组合的设备端临时文件。
    """
    local_bin = VENDOR_DIR / bin_name
    remote_bin = f"{remote_dir}/xosdbg"
    remote_jpg = f"{remote_dir}/quamm_{token}.jpg"
    if not local_bin.is_file() or not _push(addr, str(local_bin), remote_bin, timeout):
        _cleanup(addr, remote_bin, timeout=timeout)
        return "push_failed", False

    _shell(addr, f"chmod +x {remote_bin}", timeout)
    # -s/--skipcompare：跳过「framebuffer 未变化则跳过抓图」的优化，保证每次都产出 jpg。
    rc, out = _shell(
        addr,
        f"rm -f {remote_jpg}; {remote_bin} xosscreen -o {remote_jpg} "
        f"-q {quality} -r {rotate} -d {framebuffer} -s 2>&1",
        timeout,
    )
    low = out.lower()
    if "illegal instruction" in low or "sigill" in low:
        _cleanup(addr, remote_bin, remote_jpg, timeout=timeout)
        return "abi_mismatch", False
    if rc != 0:
        _cleanup(addr, remote_bin, remote_jpg, timeout=timeout)
        return "xosscreen_failed", False

    if not _pull(addr, remote_jpg, str(local_path), timeout) or not _valid_jpeg(local_path):
        local_path.unlink(missing_ok=True)
        _cleanup(addr, remote_bin, remote_jpg, timeout=timeout)
        return "no_valid_jpeg", False

    _cleanup(addr, remote_bin, remote_jpg, timeout=timeout)
    return "ok", True


def _write_record(session, result: dict, created_by: str | None) -> None:
    """v0.24：截图成功后把元数据写入 screenshot_record（显式 commit，项目约定）。"""
    from app.models import ScreenshotRecord
    from app.utils import new_id, utcnow

    rec = ScreenshotRecord(
        shot_id=new_id("shot_", 8),
        device_id=result["device_id"],
        file_name=result["file_name"],
        bytes=result["bytes"],
        quality=result["quality"],
        rotate=result["rotate"],
        float_abi=result["float_abi"],
        bin=result["bin"],
        created_by=created_by,
        captured_at=utcnow(),
    )
    session.add(rec)
    session.commit()


def list_screenshots(session, device_id: str | None = None, limit: int = 200) -> list[dict]:
    """领域函数：列出截图记录（最新在前），可按 device_id 过滤。"""
    from app.models import ScreenshotRecord
    from app.screenshot_lifecycle import serialize

    q = session.query(ScreenshotRecord)
    if device_id:
        q = q.filter(ScreenshotRecord.device_id == device_id)
    rows = q.order_by(ScreenshotRecord.captured_at.desc()).limit(limit).all()
    return [serialize(r) for r in rows]


def delete_screenshot(
    session,
    shot_id: str | None = None,
    file_name: str | None = None,
    actor: str = "system",
) -> bool:
    """领域函数：按 shot_id 或 file_name 删一条记录 + 磁盘文件（文件缺失不报错）。"""
    from app.domain import audit
    from app.models import ScreenshotRecord

    if shot_id:
        rec = session.get(ScreenshotRecord, shot_id)
    elif file_name:
        rec = (
            session.query(ScreenshotRecord)
            .filter(ScreenshotRecord.file_name == file_name)
            .first()
        )
    else:
        return False
    if rec is None:
        return False
    # 删文件：仅接受安全文件名，文件缺失/OSError 都不报错
    if _SAFE_FILENAME_RE.fullmatch(rec.file_name):
        try:
            (SCREENSHOT_DIR / rec.file_name).unlink(missing_ok=True)
        except OSError:
            pass
    session.delete(rec)
    # 与 prune 一致：所有删除写 audit（action=screenshot_prune，detail 含数量/原因）
    audit.record(
        session, actor=actor, action="screenshot_prune",
        device_id=rec.device_id,
        detail={"count": 1, "reason": "manual_delete"},
    )
    session.commit()
    return True


def capture_screenshot(
    device_id: str | None = None,
    ip: str | None = None,
    port: int | None = None,
    quality: int = 60,
    rotate: int = 0,
    framebuffer: str = "/dev/fb0",
    session=None,
    created_by: str | None = None,
    timeout: int = ADB_TIMEOUT,
) -> dict:
    """对设备截图并保存到主机，返回文件路径与元数据。

    device_id（从库读 ip/port + 机型浮点配置）或直接 ip/port 二选一。
    返回 {device_id, ip, float_abi, bin, local_path, file_name, bytes,
           quality, rotate, captured_at, float_source}。
    v0.24：成功后写一条 screenshot_record 记录，并按设备做保留策略 prune。
    """
    quality, rotate = int(quality), int(rotate)
    session, own_session = _own_session_if_needed(session)
    try:
        host, port, product_config = _resolve_target(device_id, ip, port, session)
        _validate(host, port, quality, rotate, framebuffer)
        addr = f"{host}:{port}"

        if not _connect(addr, timeout):
            _fail("SCREENSHOT_CONNECT_FAILED", f"adb 连接失败：{addr}")
        try:
            float_abi, float_source = _resolve_float_abi(addr, product_config, timeout)
            primary = HARD_BIN if float_abi == "hard" else SOFT_BIN
            alternate = SOFT_BIN if primary == HARD_BIN else HARD_BIN

            label = _SAFE_LABEL_RE.sub("_", device_id or host)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            file_name = f"{label}_{stamp}.jpg"
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            local_path = SCREENSHOT_DIR / file_name
            token = stamp

            # /data 优先（不可写/空间不足自动退 /tmp）；同目录内 ABI 不匹配时换另一个二进制重试。
            chosen = primary
            reason = "not_attempted"
            for remote_dir in REMOTE_DIRS:
                for bin_name in (primary, alternate):
                    reason, ok = _capture_once(
                        addr, remote_dir, bin_name, quality, rotate,
                        framebuffer, token, local_path, timeout,
                    )
                    if ok:
                        chosen = bin_name
                        break
                else:
                    continue
                break
            else:
                local_path.unlink(missing_ok=True)
                _fail("SCREENSHOT_FAILED", f"截图失败（{reason}）：{addr}")
        finally:
            _disconnect(addr, timeout)

        result = {
            "device_id": device_id,
            "ip": host,
            "float_abi": float_abi,
            "bin": chosen,
            "local_path": str(local_path),
            "file_name": file_name,
            "bytes": local_path.stat().st_size,
            "quality": quality,
            "rotate": rotate,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "float_source": float_source,
        }
        # v0.24：截图成功后元数据入库（显式 commit）；随后 best-effort 按设备做保留策略，
        # 只保留最新 N 条。清理失败只记警告，绝不影响已成功的截图返回。
        _write_record(session, result, created_by)
        if result["device_id"]:
            try:
                from app import screenshot_lifecycle

                screenshot_lifecycle.prune_device(
                    session, result["device_id"], actor=created_by or "system"
                )
            except Exception:  # noqa: BLE001
                logger.warning("per-device screenshot prune failed for %s", result["device_id"])
        return result
    finally:
        if own_session is not None:
            own_session.close()
