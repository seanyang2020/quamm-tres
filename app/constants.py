"""状态与枚举常量。"""
from __future__ import annotations

from pathlib import Path

# /api/version 的 name：产品展示名统一为 Quamm TRES（网页 title / MCP server name / README / 邮件）。
APP_NAME = "Quamm TRES"

# VERSION 文件相对 app 包的位置（仓库根）
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"

# 异常/待修复：pending 超过该天数未转正，列为「待清理」
PENDING_CLEANUP_DAYS = 30


def get_version() -> str:
    """读取根目录 VERSION 文件（单一真源）。缺失/为空即抛错，不静默降级。"""
    value = _VERSION_FILE.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"VERSION 文件为空：{_VERSION_FILE}")
    return value


def version_info() -> dict:
    """/api/version 的统一口径。stage 由版本前缀推导：0.x 为 pre-release。"""
    v = get_version()
    stage = "pre-release" if v.startswith("0.") else "stable"
    return {"version": v, "name": APP_NAME, "stage": stage}


class DeviceStatus:
    AVAILABLE = "available"
    OCCUPIED = "occupied"
    PENDING = "pending"                # 已登记但探不到 / 非 L3，不进可用池
    PENDING_REMOVAL = "pending_removal"  # 有活动租约，挂待下架
    RETIRED = "retired"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"

    # 可用池（可被申请）
    POOL = {AVAILABLE}


class HealthLevel:
    L0 = "L0"  # ICMP 不通
    L1 = "L1"  # 端口不通
    L2 = "L2"  # unauthorized
    L3 = "L3"  # shell 可用


class LeaseState:
    GRANTED = "GRANTED"
    EXPIRING = "EXPIRING"   # TTL 到期，宽限期内
    RESERVED = "RESERVED"   # 队首 offer 确认窗口
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"     # 宽限期结束强制回收
    FORCED = "FORCED"       # 管理员强制回收

    # 仍占用设备的活跃态（参与并发上限与唯一性）
    ACTIVE = {GRANTED, EXPIRING, RESERVED}


class QueueState:
    QUEUED = "QUEUED"
    OFFERED = "OFFERED"
    GRANTED = "GRANTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class NoticeStatus:
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    FAILED = "failed"
    TIMEOUT = "timeout"
    ACKED = "acked"


class NoticeType:
    DEVICE_OFFLINE = "device_offline"
    DEVICE_RECOVERED = "device_recovered"
    DEVICE_UNREGISTERED = "device_unregistered"
    PATROL_STALLED = "patrol_stalled"
    CREDENTIAL_EXPIRING = "credential_expiring"
    LEASE_EXPIRED = "lease_expired"
    QUEUE_WOKEN = "queue_woken"


class RegisterChannel:
    ADMIN_WEB = "admin_web"
    MCP_AGENT = "mcp_agent"


# 可申请状态（available 且在池内、L3）
def is_claimable(status: str, health: str, allow_suspect: bool = False) -> bool:
    """设备是否可被申请。

    - status 必须是 available（offline/pending/occupied 都不行）
    - L3（ok）始终可申请
    - L1/L2（degraded/suspect）默认不可申请；allow_suspect=True 时才放行
    - L0（不可达）永远不可申请，即使打开降级开关
    """
    if status != DeviceStatus.AVAILABLE:
        return False
    if health == HealthLevel.L3:
        return True
    if allow_suspect and health in (HealthLevel.L1, HealthLevel.L2):
        return True
    return False
