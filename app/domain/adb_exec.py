"""ADB 调试执行：adb connect + adb shell（人工调试，非命令白名单）。

鉴权对齐 serial.check_access：admin 或设备活跃租约持有人。
明显破坏性命令需 body.confirm=true，否则 ADB_CONFIRM_REQUIRED。
"""
from __future__ import annotations

import re
import subprocess
import time
from typing import Any

from sqlalchemy.orm import Session

from app.domain import audit
from app.domain import serial as serial_svc
from app.domain.catalog import DomainError
from app.models import Device

ADB_BIN = "adb"
ADB_OUTPUT_LIMIT = 64 * 1024
ADB_CMD_AUDIT_LIMIT = 512
ADB_TIMEOUT_MS_DEFAULT = 15000
ADB_TIMEOUT_MS_MIN = 1000
ADB_TIMEOUT_MS_MAX = 60000

# 命中且无 confirm 时拒绝执行（可调常量）
ADB_DANGEROUS_RE = re.compile(
    r"(?:\breboot\b|\brm\s|\bmkfs\b|\bfastboot\b|\bflash\b|\bwipe\b|\bformat\b)",
    re.IGNORECASE,
)


def _truncate(text: str, limit: int = ADB_OUTPUT_LIMIT) -> tuple[str, bool]:
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _clamp_timeout_ms(timeout_ms: int | None) -> int:
    if timeout_ms is None:
        return ADB_TIMEOUT_MS_DEFAULT
    try:
        v = int(timeout_ms)
    except (TypeError, ValueError) as e:
        raise DomainError(
            "ADB_BAD_PAYLOAD",
            f"timeout_ms 非法: {timeout_ms!r}",
        ) from e
    if v < ADB_TIMEOUT_MS_MIN or v > ADB_TIMEOUT_MS_MAX:
        raise DomainError(
            "ADB_BAD_PAYLOAD",
            f"timeout_ms 须在 {ADB_TIMEOUT_MS_MIN}–{ADB_TIMEOUT_MS_MAX}",
            {"timeout_ms": v},
        )
    return v


def _requires_confirm(command: str) -> bool:
    return bool(ADB_DANGEROUS_RE.search(command or ""))


def _run_adb(args: list[str], timeout_s: float) -> tuple[int, str, str]:
    """跑一条 adb；GNU timeout + subprocess 双保险。返回 (rc, stdout, stderr)。

    rc=124 表示超时（与 GNU timeout 约定一致）。
    """
    timeout_s = max(1.0, float(timeout_s))
    # GNU timeout 用整数秒；不足 1s 已抬到 1
    gnu_s = max(1, int(round(timeout_s)))
    full = ["timeout", str(gnu_s), ADB_BIN, *args]
    try:
        proc = subprocess.run(
            full,
            capture_output=True,
            text=True,
            timeout=gnu_s + 2,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "adb 超时"
    except FileNotFoundError:
        return 127, "", f"{ADB_BIN} 未找到"
    except OSError as e:
        return 127, "", str(e)
    return (
        int(proc.returncode or 0),
        proc.stdout or "",
        proc.stderr or "",
    )


def _looks_unauthorized(*texts: str) -> bool:
    blob = " ".join(texts).lower()
    return "unauthorized" in blob or "offline" in blob


def _require_access(
    session: Session,
    device_id: str,
    *,
    email: str,
    is_admin: bool,
) -> None:
    try:
        serial_svc.check_access(
            session,
            device_id,
            email=email,
            is_admin=is_admin,
        )
    except DomainError as e:
        if e.code in ("NO_LEASE", "FORBIDDEN", "ACCESS_DENIED"):
            raise DomainError(
                "ADB_NOT_ALLOWED",
                f"无权限在 {device_id} 上执行 ADB（需管理员或该设备活跃租约）",
                {"device_id": device_id, "cause": e.code},
            ) from e
        raise


def _audit_exec(
    session: Session,
    *,
    actor: str,
    device_id: str,
    command: str,
    rc: int | None,
    duration_ms: int,
    result: str,
    error_code: str | None = None,
) -> None:
    audit.record(
        session,
        actor=actor,
        action="adb_exec",
        device_id=device_id,
        detail={
            "command": (command or "")[:ADB_CMD_AUDIT_LIMIT],
            "rc": rc,
            "duration_ms": duration_ms,
            "result": result,
            "error_code": error_code,
        },
    )
    session.commit()


def exec_shell(
    session: Session,
    device_id: str,
    *,
    command: str,
    timeout_ms: int | None = None,
    confirm: bool = False,
    by: str,
    email: str,
    is_admin: bool,
) -> dict[str, Any]:
    """对设备执行 ``adb connect`` + ``adb -s <addr> shell <command>``。"""
    cmd = str(command or "").strip()
    if not cmd:
        raise DomainError("ADB_BAD_PAYLOAD", "command 必填")

    device = session.get(Device, device_id)
    if device is None:
        raise DomainError("NOT_FOUND", f"设备不存在: {device_id}")

    addr = device.adb_address
    if not addr:
        raise DomainError(
            "ADB_NO_ENDPOINT",
            f"{device_id} 无 adb_address（缺 ip）",
            {"device_id": device_id},
        )

    _require_access(session, device_id, email=email, is_admin=is_admin)

    if _requires_confirm(cmd) and not confirm:
        raise DomainError(
            "ADB_CONFIRM_REQUIRED",
            "该命令可能破坏性较大，请传 confirm=true 后重试",
            {"command": cmd[:ADB_CMD_AUDIT_LIMIT]},
        )

    timeout_ms_v = _clamp_timeout_ms(timeout_ms)
    timeout_s = timeout_ms_v / 1000.0
    t0 = time.monotonic()

    # 1) connect（已连接时 adb 通常幂等成功）
    crc, cout, cerr = _run_adb(["connect", addr], timeout_s=min(timeout_s, 15.0))
    connect_blob = f"{cout}\n{cerr}"
    if crc == 124:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _audit_exec(
            session, actor=by, device_id=device_id, command=cmd,
            rc=124, duration_ms=duration_ms, result="fail",
            error_code="ADB_TIMEOUT",
        )
        raise DomainError("ADB_TIMEOUT", f"adb connect {addr} 超时")
    if _looks_unauthorized(cout, cerr):
        duration_ms = int((time.monotonic() - t0) * 1000)
        _audit_exec(
            session, actor=by, device_id=device_id, command=cmd,
            rc=crc, duration_ms=duration_ms, result="fail",
            error_code="ADB_UNAUTHORIZED",
        )
        raise DomainError(
            "ADB_UNAUTHORIZED",
            f"设备 {addr} unauthorized/offline",
            {"stdout": cout[:512], "stderr": cerr[:512]},
        )
    low = connect_blob.lower()
    if crc != 0 and "connected" not in low and "already connected" not in low:
        duration_ms = int((time.monotonic() - t0) * 1000)
        _audit_exec(
            session, actor=by, device_id=device_id, command=cmd,
            rc=crc, duration_ms=duration_ms, result="fail",
            error_code="ADB_CONNECT_FAILED",
        )
        raise DomainError(
            "ADB_CONNECT_FAILED",
            f"adb connect {addr} 失败 (rc={crc})",
            {"stdout": cout[:512], "stderr": cerr[:512]},
        )

    # 2) shell
    remaining = max(1.0, timeout_s - (time.monotonic() - t0))
    src, sout, serr = _run_adb(["-s", addr, "shell", cmd], timeout_s=remaining)
    duration_ms = int((time.monotonic() - t0) * 1000)

    if src == 124:
        _audit_exec(
            session, actor=by, device_id=device_id, command=cmd,
            rc=124, duration_ms=duration_ms, result="fail",
            error_code="ADB_TIMEOUT",
        )
        raise DomainError("ADB_TIMEOUT", f"adb shell 超时（{timeout_ms_v}ms）")

    if _looks_unauthorized(sout, serr):
        _audit_exec(
            session, actor=by, device_id=device_id, command=cmd,
            rc=src, duration_ms=duration_ms, result="fail",
            error_code="ADB_UNAUTHORIZED",
        )
        raise DomainError(
            "ADB_UNAUTHORIZED",
            f"设备 {addr} unauthorized/offline",
            {"stdout": sout[:512], "stderr": serr[:512]},
        )

    out, out_cut = _truncate(sout)
    err, err_cut = _truncate(serr)
    truncated = out_cut or err_cut
    result = "ok" if src == 0 else "fail"

    _audit_exec(
        session, actor=by, device_id=device_id, command=cmd,
        rc=src, duration_ms=duration_ms, result=result,
    )

    return {
        "result": result,
        "rc": src,
        "stdout": out,
        "stderr": err,
        "duration_ms": duration_ms,
        "truncated": truncated,
    }
