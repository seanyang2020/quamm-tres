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

    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adb_port: Mapped[int] = mapped_column(Integer, default=5555, nullable=False)
    android_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    build_fingerprint: Mapped[str | None] = mapped_column(String(256), nullable=True)

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

    device: Mapped["Device"] = relationship()
    principal: Mapped["Principal"] = relationship()

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


class Setting(Base):
    """系统配置 KV 表（v0.11）：SMTP 等可在线修改的配置。"""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime())
