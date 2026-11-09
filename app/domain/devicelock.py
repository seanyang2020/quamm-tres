"""设备侧 RAMFS 文件锁（v0.18）。

在设备 tmpfs（默认 /tmp/quamm_tres.lock，重启清空）上写一把 JSON 锁，作为设备级
物理闸门。设计口径：

- 时间一律用主机侧可信时钟（utcnow）生成后随锁 push，绝不读设备系统时间；
- 锁由 Agent 自己维护：每次操作前检查，缺失就用本地副本重推，foreign lease 不自行删除；
- 服务端回收时校验 holder/lease_id 匹配后删锁，并 adb disconnect（兜底，不依赖 Agent）。

写入用 adb push 本地临时文件，避免 shell 引号问题；所有命令带 GNU timeout 与
subprocess 超时双保险，不挂起。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

from app.config import settings
from app.utils import utcnow

logger = logging.getLogger("quamm-tres.devicelock")

LOCK_VERSION = 1
ADB_TIMEOUT = 8  # push/rm 比纯读稍慢

# 物理闸门总开关：生产默认开启（回收即删锁+断 ADB）；单元测试在 conftest 关闭，
# 避免在无设备环境里反复跑真实 adb 超时。
physical_gate_enabled = True


def _run(cmd: list[str]) -> tuple[int, str]:
    full = ["timeout", str(ADB_TIMEOUT), "adb", *cmd]
    try:
        proc = subprocess.run(
            full, capture_output=True, text=True,
            timeout=ADB_TIMEOUT + 1, check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except (FileNotFoundError, OSError):
        return 127, ""
    return proc.returncode, (proc.stdout or "").strip()


def build_payload(
    *,
    lease_id: str,
    device_id: str,
    holder_email: str,
    holder_name: str | None,
    token_prefix: str | None,
    agent_info: str | None,
    now=None,
) -> dict:
    """在主机侧拼锁内容（主机时钟），不写完整 token，只放前缀。"""
    ts = (now or utcnow()).isoformat()
    return {
        "lock_version": LOCK_VERSION,
        "lease_id": lease_id,
        "device_id": device_id,
        "holder_email": holder_email,
        "holder_name": holder_name or "",
        "token_prefix": token_prefix or "",
        "agent_info": agent_info or "",
        "acquired_at": ts,
        "heartbeat_at": ts,
    }


def _connect(adb_address: str) -> None:
    _run(["connect", adb_address])


def write_lock(adb_address: str, payload: dict, lock_path: str | None = None) -> bool:
    """adb push 本地临时文件到设备锁路径。成功返回 True。"""
    path = lock_path or settings.device_lock_path
    _connect(adb_address)
    fd, tmp = tempfile.mkstemp(prefix="quamm_lock_", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        rc, _ = _run(["-s", adb_address, "push", tmp, path])
        return rc == 0
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def read_lock(adb_address: str, lock_path: str | None = None) -> dict | None:
    """读设备锁并解析 JSON；不存在/损坏返回 None。"""
    path = lock_path or settings.device_lock_path
    _connect(adb_address)
    rc, out = _run(["-s", adb_address, "shell", "cat", path])
    if rc != 0 or not out:
        return None
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def remove_lock(
    adb_address: str, expected_lease_id: str | None = None,
    lock_path: str | None = None,
) -> bool:
    """删锁；给定期望 lease_id 时先校验匹配再删，避免误删别人的锁。"""
    path = lock_path or settings.device_lock_path
    _connect(adb_address)
    if expected_lease_id is not None:
        current = read_lock(adb_address, path)
        if current is not None and current.get("lease_id") != expected_lease_id:
            logger.warning(
                "remove_lock aborted: expected %s but device holds %s",
                expected_lease_id, current.get("lease_id"),
            )
            return False
    rc, _ = _run(["-s", adb_address, "shell", "rm", "-f", path])
    return rc == 0


def disconnect(adb_address: str) -> bool:
    rc, _ = _run(["disconnect", adb_address])
    return rc == 0
