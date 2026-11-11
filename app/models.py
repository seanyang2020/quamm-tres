"""SQLAlchemy 2.0 ORM 模型（第一阶段 10 张表）。

关键约定（见 docs/04 G1）：
- device 用内部 device_id 做主键，MAC/SN/asset_tag/position_code 建唯一索引，不用 MAC 做主键。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.constants import (
    DeviceStatus,
    HealthLevel,
    LeaseState,
    NoticeStatus,
    NoticeType,
    QueueState,
    RegisterChannel,
)
from app.db import Base


class ProductType(Base):
    """机型字典：预置、有限。"""

    __tablename__ = "product_type"

    type_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    has_screen: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_led: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    serial_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    adb_port: Mapped[int] = mapped_column(Integer, default=5555, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    model_pattern: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # v0.12：工程/编译元数据（可为空，四列齐全才算「完整」）。
    defconfig: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sdk_repo_cmd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    build_cmd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    jenkins_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v0.14：固件策略。db_project_name 对应 DB 服务器 Daily/XOS/<name>/；
    # required_* 为最低可分配版本（0 = 不强制），对应 semver X.Y.Z。
    db_project_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    required_major: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    required_minor: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    required_patch: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    # v0.23：芯片/浮点能力（设备截图据此选 xosdbg=soft / vxosdbg=hard）。
    # 取值 hard/soft/auto；auto 表示由 /proc/cpuinfo 或运行时兜底推断。
    chip_model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    float_abi: Mapped[str | None] = mapped_column(
        String(16), default="auto", server_default="auto", nullable=True
    )
    has_fpu: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    # v0.25：编译服务器引用（SSH 凭据集中存在 build_server，机型只引用）
    build_server_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("build_server.server_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime())


class Device(Base):
    __tablename__ = "device"

    device_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    model_class: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("product_type.type_code"), nullable=True
    )
    # 身份锚点（唯一索引，可退役声明二期再做 identity_claim）
    mac: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sn: Mapped[str | None] = mapped_column(String(128), nullable=True)
    asset_tag: Mapped[str | None] = mapped_column(String(128), nullable=True)
    position_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    owner: Mapped[str] = mapped_column(String(128), nullable=False)  # 维护责任人
    status: Mapped[str] = mapped_column(String(32), default=DeviceStatus.PENDING, nullable=False)
    health_level: Mapped[str] = mapped_column(String(8), default=HealthLevel.L0, nullable=False)
    registered_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    register_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # v0.19：注册该设备的 Agent 名称（agent_info），设备页直接可见
    register_agent: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # v0.26 设备共享范围与允许名单（名单为 JSON 字符串数组）
    share_scope: Mapped[str] = mapped_column(
        String(16), default="shared", server_default="shared", nullable=False
    )
    allowed_emails: Mapped[str | None] = mapped_column(Text, nullable=True)
    allowed_agent_labels: Mapped[str | None] = mapped_column(Text, nullable=True)

    # v0.27 手动锁定：锁定后不参与任何分配（人工闸门，与 health/status 无关）
    locked: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adb_port: Mapped[int] = mapped_column(Integer, default=5555, nullable=False)
    android_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    build_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)
    # v0.14：设备当前固件版本（semver 风格，探测/刷机成功后回填）
    firmware_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 真实 XOS 设备指纹 / 工程配置名 / 数据来源（mock=模拟 HTTP，real=真实 adb）
    project_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    board_dt: Mapped[str | None] = mapped_column(String(256), nullable=True)
    storage_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    has_wifi: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source: Mapped[str] = mapped_column(
        String(16), default="mock", server_default="mock", nullable=False
    )

    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())

    product_type: Mapped["ProductType | None"] = relationship()

    @property
    def adb_address(self) -> str | None:
        if not self.ip:
            return None
        return f"{self.ip}:{self.adb_port}"

    __table_args__ = (
        Index("uq_device_mac", "mac", unique=True),
        Index("uq_device_sn", "sn", unique=True),
        Index("uq_device_asset_tag", "asset_tag", unique=True),
        Index("uq_device_position_code", "position_code", unique=True),
    )


class Principal(Base):
    """名单（roster）：人 / 服务账号。配额挂在人这一层。"""

    __tablename__ = "principal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(256), unique=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    employee_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quota: Mapped[int] = mapped_column(Integer, default=2, nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime())

    credentials: Mapped[list["Credential"]] = relationship(back_populates="principal")


class Credential(Base):
    """凭据：rr_ 前缀 token，只存 SHA-256 哈希，90 天有效期。"""

    __tablename__ = "credential"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principal.id"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    agent_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    # v0.11：凭据自带权限档位（admin/user）与备注，签发时确定，一人可有多条。
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user", nullable=False)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())

    principal: Mapped["Principal"] = relationship(back_populates="credentials")

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def active(self) -> bool:
        from app.utils import utcnow

        return not self.revoked and (self.expires_at is None or self.expires_at > utcnow())


class Lease(Base):
    __tablename__ = "lease"

    lease_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("device.device_id"), nullable=False)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principal.id"), nullable=False)
    credential_id: Mapped[int | None] = mapped_column(ForeignKey("credential.id"), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(256), nullable=True)
    ttl_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default=LeaseState.GRANTED, nullable=False)
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adb_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())
    # v0.18：最近一次心跳时间，worker 据此判定 Agent 是否仍在使用
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    device: Mapped["Device"] = relationship()
    principal: Mapped["Principal"] = relationship()
    credential: Mapped["Credential"] = relationship(foreign_keys=[credential_id])

    # 活跃租约每台设备至多一条（数据库层防并发超发）
    __table_args__ = (
        Index(
            "uq_lease_active_device",
            "device_id",
            unique=True,
            sqlite_where=state.in_(["GRANTED", "EXPIRING", "RESERVED"]),
            postgresql_where=state.in_(["GRANTED", "EXPIRING", "RESERVED"]),
        ),
    )


class QueueEntry(Base):
    __tablename__ = "queue_entry"

    entry_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # product / device
    target_code: Mapped[str] = mapped_column(String(64), nullable=False)
    principal_id: Mapped[int] = mapped_column(ForeignKey("principal.id"), nullable=False)
    credential_id: Mapped[int | None] = mapped_column(ForeignKey("credential.id"), nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(256), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state: Mapped[str] = mapped_column(String(16), default=QueueState.QUEUED, nullable=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime())
    offered_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    offer_expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # offer 或 grant 关联的租约
    created_at: Mapped[datetime] = mapped_column(DateTime())

    principal: Mapped["Principal"] = relationship()

    __table_args__ = (
        Index("ix_queue_target", "target_type", "target_code", "state"),
    )


class AuditEvent(Base):
    __tablename__ = "audit_event"

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)  # email 或 'system'
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    lease_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())

    __table_args__ = (Index("ix_audit_device", "device_id"), Index("ix_audit_actor", "actor"))


class Notice(Base):
    __tablename__ = "notice"

    notice_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    severity: Mapped[str] = mapped_column(String(16), default="info", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime())

    receipts: Mapped[list["NoticeReceipt"]] = relationship(back_populates="notice")


class NoticeReceipt(Base):
    __tablename__ = "notice_receipt"

    receipt_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notice_id: Mapped[str] = mapped_column(ForeignKey("notice.notice_id"), nullable=False)
    recipient: Mapped[str] = mapped_column(String(256), nullable=False)  # email
    channel: Mapped[str] = mapped_column(String(32), default="web", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default=NoticeStatus.PENDING, nullable=False)
    error: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    notice: Mapped["Notice"] = relationship(back_populates="receipts")


class PatrolRun(Base):
    __tablename__ = "patrol_run"

    run_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scanned_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    l3_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suspect_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    offline_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failures: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())


class Contact(Base):
    """人员通讯录（v0.11）：email 主键，name / note 供凭据与租约展示。"""

    __tablename__ = "contacts"

    email: Mapped[str] = mapped_column(String(256), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    note: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())


class FlashJob(Base):
    """固件刷机任务（v0.14）：异步执行，状态落库，进程重启可查历史。

    status: QUEUED / RUNNING / SUCCEEDED / FAILED
    """

    __tablename__ = "flash_job"

    job_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    lease_id: Mapped[str] = mapped_column(String(32), nullable=False)
    device_id: Mapped[str] = mapped_column(String(32), nullable=False)
    product_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    preserve_data: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED", nullable=False)
    log_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    __table_args__ = (
        Index("ix_flash_job_lease", "lease_id", "status"),
        Index("ix_flash_job_device", "device_id"),
    )


class Setting(Base):
    """系统配置 KV 表（v0.11）：SMTP 等可在线修改的配置。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime())


class AgentFeedback(Base):
    """Agent 意见收集（v0.20）：Agent 经 MCP 提交对系统的意见/建议，管理员在设置内批判性采纳。"""

    __tablename__ = "agent_feedback"

    feedback_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    credential_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("credential.id"), nullable=True, index=True
    )
    agent_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    category: Mapped[str] = mapped_column(String(32), nullable=False)  # bug/improvement/question/praise
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False, index=True)
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class ScreenshotRecord(Base):
    """设备截图元数据（v0.24）：一行对应 app/var/screenshots 下的一个 jpg。

    全新表，由 init_db() 的 create_all 自动建；migrations._ensure_screenshot_table
    用 inspect 判表做幂等兜底（新表无需 ALTER，不涉及加列）。
    """

    __tablename__ = "screenshot_record"

    shot_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # shot_+hex
    device_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    file_name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    quality: Mapped[int] = mapped_column(Integer, nullable=False)
    rotate: Mapped[int] = mapped_column(Integer, nullable=False)
    float_abi: Mapped[str] = mapped_column(String(8), nullable=False)
    bin: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)

class BuildServer(Base):
    """编译服务器（v0.25）：SSH 主机 + 加密凭据，供 SDK 拉代码/编译/升级。

    密钥（密码或私钥）经 app.secret_crypto  Fernet 加密后存 secret_encrypted，
    主密钥不在本库；接口只回显掩码，服务端经 remote_ssh 集中使用、不下发 agent。
    """

    __tablename__ = "build_server"

    server_id: Mapped[str] = mapped_column(String(32), primary_key=True)  # bs_+hex
    # v0.25：账号级归属（一账号一台，unique）；name 仅作展示标签，不再全局唯一
    principal_id: Mapped[int | None] = mapped_column(
        ForeignKey("principal.id"), unique=True, nullable=True
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    host: Mapped[str] = mapped_column(String(128), nullable=False)
    port: Mapped[int] = mapped_column(Integer, default=22, server_default="22", nullable=False)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    auth_mode: Mapped[str] = mapped_column(
        String(8), default="password", server_default="password", nullable=False
    )  # password / key
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_root: Mapped[str] = mapped_column(
        String(128), default="tres-auto", server_default="tres-auto", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default="unknown", nullable=False
    )  # unknown / available / unavailable
    probe_detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    last_probe_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime())
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


# ---------------- v0.29 设备层串口能力 ----------------
# 物理串口由 tres-logs 日志机器以 raw TCP 暴露；TRES 只存"设备↔端口↔动作"的定义与状态。
# 表结构见 docs：D:\work\aitools\private\Quamm-TRES-设备串口能力规划.md §3


class DeviceSerialPort(Base):
    """一台设备的一路串口（设备级配置，同机型不同接线各自配）。"""

    __tablename__ = "device_serial_port"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("device.device_id"), nullable=False
    )
    slot: Mapped[int] = mapped_column(Integer, nullable=False)  # 第几路 1..N
    role: Mapped[str] = mapped_column(
        String(16), default="spare", server_default="spare", nullable=False
    )  # log | power | mode | spare
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transport: Mapped[str] = mapped_column(
        String(8), default="tcp", server_default="tcp", nullable=False
    )  # tcp（tres-logs）| local（COM/tty）
    endpoint: Mapped[str | None] = mapped_column(String(128), nullable=True)  # tcp://host:port
    port_name: Mapped[str | None] = mapped_column(String(64), nullable=True)  # COM11 / /dev/ttyUSB0
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True)  # tres-logs 侧通道名
    baudrate: Mapped[int] = mapped_column(
        Integer, default=115200, server_default="115200", nullable=False
    )
    databits: Mapped[int] = mapped_column(Integer, default=8, server_default="8", nullable=False)
    stopbits: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    parity: Mapped[str] = mapped_column(String(1), default="N", server_default="N", nullable=False)
    encoding_default: Mapped[str] = mapped_column(
        String(8), default="hex", server_default="hex", nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime())

    __table_args__ = (
        Index("uq_serial_port_slot", "device_id", "slot", unique=True),
    )


class SerialAction(Base):
    """一个语义动作 = 有序多条命令（steps 为 JSON 数组）。"""

    __tablename__ = "serial_action"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    port_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("device_serial_port.id"), nullable=False
    )
    action_key: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(
        String(16), default="custom", server_default="custom", nullable=False
    )  # power | mode | custom
    steps: Mapped[list | None] = mapped_column(JSON, nullable=True)
    inter_step_ms: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="1", nullable=False
    )
    sort: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    __table_args__ = (
        Index("uq_serial_action_key", "port_id", "action_key", unique=True),
    )


class SerialState(Base):
    """设备串口最后状态（供能力声明与排障，不做乐观推断）。"""

    __tablename__ = "serial_state"

    device_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("device.device_id"), primary_key=True
    )
    power_state: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default="unknown", nullable=False
    )  # on | off | unknown
    mode_state: Mapped[str] = mapped_column(
        String(16), default="unknown", server_default="unknown", nullable=False
    )
    last_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_result: Mapped[str | None] = mapped_column(String(16), nullable=True)  # ok|fail|timeout|busy
    last_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)


class SerialLogCursor(Base):
    """log 通道读游标（同一 log 串口只能 tres-logs 独占采集，TRES 只记读到哪）。"""

    __tablename__ = "serial_log_cursor"

    device_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    port_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_offset: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_read_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
