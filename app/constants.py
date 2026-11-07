"""状态与枚举常量。"""


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
def is_claimable(status: str, health: str) -> bool:
    return status == DeviceStatus.AVAILABLE and health == HealthLevel.L3
