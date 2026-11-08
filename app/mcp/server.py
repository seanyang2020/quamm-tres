"""MCP Server：quamm-tres（21 个工具，streamable HTTP，单实例）。

鉴权由 McpAuthMiddleware 完成；工具内 require_auth() 取当前身份。
res_admin 仅管理员可用（运行时校验），普通 token 调用返回 FORBIDDEN。
"""
from __future__ import annotations

from fastmcp import FastMCP

from app.config import settings
from app.constants import get_version
from app.db import SessionLocal
from app.domain import catalog, flash, leasing
from app.domain.catalog import DomainError
from app.models import Credential, Device, Lease, Notice, PatrolRun, Principal, QueueEntry
from app.security import AuthError, require_auth
from app.utils import utcnow

mcp = FastMCP(
    "quamm-tres",
    instructions=(
        "测试设备资源分配中心。先 res_apply 拿租约和 adb_address，再 adb connect，"
        "用完 res_release。长任务到期前调 res_renew 续约。"
    ),
)


def _err(e: DomainError) -> dict:
    return {"error": e.code, "message": e.message, **(e.detail or {})}


def _guard(fn, *args, **kwargs):
    """带身份调用领域函数（session + auth）。"""
    try:
        auth = require_auth()
        session = SessionLocal()
        try:
            return fn(session, auth, *args, **kwargs)
        finally:
            session.close()
    except DomainError as e:
        return _err(e)
    except AuthError as e:
        return {"error": "AUTH", "message": e.message}


def _guard_ro(fn, *args, **kwargs):
    """只读调用领域函数（仅 session）。"""
    try:
        session = SessionLocal()
        try:
            return fn(session, *args, **kwargs)
        finally:
            session.close()
    except DomainError as e:
        return _err(e)


# ---------- 只读 / 目录 ----------

@mcp.tool()
def res_list_products(keyword: str | None = None, only_available: bool = False) -> list[dict]:
    """查看机型字典及各机型余量（总数/可用/占用）。"""
    return _guard_ro(catalog.list_products, keyword=keyword, only_available=only_available)


@mcp.tool()
def res_list_devices(
    model_class: str | None = None,
    status: str | None = None,
    limit: int = 100,
    claimable_only: bool = True,
) -> list[dict]:
    """列出设备（脱敏：不含占用人身份）。可按机型/状态过滤。

    默认只返回当前真正可申请的设备（available 且探测健康），
    suspect/offline 设备不会出现在结果里；排查时可传 claimable_only=false。
    """
    return _guard_ro(
        catalog.list_devices,
        model_class=model_class,
        status=status,
        limit=limit,
        for_agent=True,
        claimable_only=claimable_only,
    )


@mcp.tool()
def res_device_info(device_id: str) -> dict:
    """单台设备详情：机型、位置、状态、健康级别、当前占用（脱敏）。"""
    return _guard_ro(catalog.device_info, device_id, for_agent=True)


@mcp.tool()
def res_my_leases() -> list[dict]:
    """我当前持有的所有活跃租约（含 adb_address 与剩余时间）。"""
    return _guard(leasing.my_leases)


# ---------- 申请 / 续约 / 释放 ----------

@mcp.tool()
def res_apply(
    product_type: str | None = None,
    device_id: str | None = None,
    target: str | None = None,
    purpose: str | None = None,
    expected_minutes: int | None = None,
    priority: int = 0,
) -> dict:
    """申请设备（两种方式二选一）。

    - product_type="moto_ld"：按机型池申请，该机型任意 available 设备空闲即分配；
      全被占则进该机型的等待队列（同机型的人共用一个队列）。
    - device_id="xxx"：指定具体某台设备（兼容旧用法）。
    - target="xxx"：旧单参数用法（device_id 或 type_code），保留兼容历史客户端。

    返回 GRANTED（含 adb_address）或 QUEUED（含 ticket_id 与位次）。
    """
    return _guard(
        leasing.apply,
        target=target,
        product_type=product_type,
        device_id=device_id,
        purpose=purpose,
        expected_minutes=expected_minutes,
        priority=priority,
    )


@mcp.tool()
def res_renew(lease_id: str, extra_minutes: int = 30) -> dict:
    """续约：延长租约到期时间。宽限期内续约可恢复 ACTIVE。"""
    return _guard(leasing.renew, lease_id, extra_minutes=extra_minutes)


@mcp.tool()
def res_release(lease_id: str, reason: str | None = None) -> dict:
    """释放租约（幂等，重复释放返回 ok）。"""
    return _guard(leasing.release, lease_id, reason=reason)


# ---------- 排队 ----------

@mcp.tool()
def res_ticket(ticket_id: str) -> dict:
    """查询排队票据状态。QUEUED(pos)/OFFERED(自动 confirm)/GRANTED/CANCELLED。"""
    return _guard(leasing.ticket, ticket_id)


@mcp.tool()
def res_ticket_cancel(ticket_id: str) -> dict:
    """放弃排队。"""
    return _guard(leasing.ticket_cancel, ticket_id)


# ---------- 身份 / 健康 ----------

@mcp.tool()
def res_whoami() -> dict:
    """我是谁：邮箱、配额、已用几台、凭据有效期。"""
    auth = require_auth()
    session = SessionLocal()
    try:
        active = leasing.count_active_leases(session, auth.principal.id)
        return {
            "email": auth.email,
            "name": auth.principal.name,
            "is_admin": auth.is_admin,
            "quota": auth.principal.quota,
            "active_leases": active,
            "credential_expires_at": auth.credential.expires_at.isoformat() if auth.credential.expires_at else None,
            "credential_revoked": auth.credential.revoked,
        }
    finally:
        session.close()


@mcp.tool()
def res_health() -> dict:
    """服务健康：MCP 状态、版本、最近一轮巡检时间、数据新鲜度。"""
    session = SessionLocal()
    try:
        last = session.query(PatrolRun).order_by(PatrolRun.ended_at.desc()).first()
        now = utcnow()
        fresh = None
        if last and last.ended_at:
            fresh = int((now - last.ended_at).total_seconds())
        return {
            "status": "ok",
            "version": get_version(),
            "last_patrol_at": last.ended_at.isoformat() if last and last.ended_at else None,
            "data_age_seconds": fresh,
        }
    finally:
        session.close()


# ---------- 设备注册（B 域） ----------

@mcp.tool()
def res_device_probe(
    ip: str,
    port: int = 5555,
    hint_model: str | None = None,
    hint_position: str | None = None,
) -> dict:
    """探测设备 IP 并出注册表（两段式第一步）。返回 form_id，30 分钟有效。"""
    return _guard_ro(
        catalog.device_probe,
        ip,
        port=port,
        hint_model=hint_model,
        hint_position=hint_position,
    )


@mcp.tool()
def res_device_register(
    form_id: str,
    position_code: str,
    owner: str,
    model_class: str | None = None,
    note: str | None = None,
) -> dict:
    """提交注册表（两段式第二步）。位置编号与责任人缺失会拒绝。"""
    auth = require_auth()
    session = SessionLocal()
    try:
        return catalog.device_register(
            session, form_id, position_code, owner,
            actor_email=auth.email, model_class=model_class, note=note,
        )
    except DomainError as e:
        return _err(e)
    finally:
        session.close()


@mcp.tool()
def res_device_flash(form_id: str, duration_sec: int = 8) -> dict:
    """让设备亮起来（屏幕显示 4 位码 / LED 闪灯）。无屏无灯机型跳过。"""
    return _guard_ro(catalog.device_flash, form_id, duration_sec=duration_sec)


@mcp.tool()
def res_device_confirm(flash_id: str, code: str) -> dict:
    """回填点亮确认码。"""
    try:
        return catalog.device_confirm(flash_id, code)
    except DomainError as e:
        return _err(e)


@mcp.tool()
def res_device_unregister(device_id: str) -> dict:
    """注销设备（仅注册人或管理员）。有活动租约会被拒绝并标 pending_removal。"""
    auth = require_auth()
    session = SessionLocal()
    try:
        return catalog.device_unregister(session, device_id, actor_email=auth.email, is_admin=auth.is_admin)
    except DomainError as e:
        return _err(e)
    finally:
        session.close()


# ---------- 固件（v0.14） ----------

@mcp.tool()
def res_firmware_versions(product_type: str, limit: int = 20) -> dict:
    """列出机型在 DB 服务器上的可用固件版本（按构建时间倒序）。

    数据源与 dbxostest 相同：Daily/XOS/<db_project_name>/ 的版本目录。
    机型未配置 db_project_name -> PRODUCT_NO_DB_PROJECT；服务器不可达 -> SOURCE_UNAVAILABLE。
    """
    return _guard_ro(flash.list_versions, product_type=product_type, limit=limit)


@mcp.tool()
def res_flash(
    lease_id: str,
    version: str | None = None,
    preserve_data: bool = True,
    confirm_erase_data: bool = False,
) -> dict:
    """异步刷机（5-15 分钟）：立即返回 job_id，用 res_flash_status 轮询。

    - 仅 GRANTED 租约、仅租约持有人（或管理员）
    - preserve_data=True（默认）保留 data 分区用户数据；False 必须同时 confirm_erase_data=True
    - version 为空则刷该机型最新版本；同一租约已有任务 -> FLASH_IN_PROGRESS
    """
    return _guard(
        flash.start_flash,
        lease_id,
        version=version,
        preserve_data=preserve_data,
        confirm_erase_data=confirm_erase_data,
    )


@mcp.tool()
def res_flash_status(job_id: str) -> dict:
    """查询刷机任务：QUEUED / RUNNING / SUCCEEDED / FAILED + 日志最后 50 行。"""
    return _guard(flash.flash_status, job_id)


@mcp.tool()
def res_firmware_status(device_id: str) -> dict:
    """查询设备固件版本是否满足机型要求（semver X.Y.Z 门禁与升级建议）。"""
    return _guard_ro(flash.firmware_status, device_id)


# ---------- 管理 ----------

@mcp.tool()
def res_admin(action: str, lease_id: str | None = None, reason: str | None = None, device_id: str | None = None) -> dict:
    """管理员工具（普通 token 不可用）。

    action 枚举：
    - force_release：强制回收租约（需 lease_id，reason 必填）
    - overview：设备池概览（total/available/suspect/occupied/offline/active_leases）
    - stats：汇总统计（设备/租约/队列/通知四组计数）
    - list_credentials：列出全部凭据（token 尾号/有效期/是否吊销）
    """
    auth = require_auth()
    if not auth.is_admin:
        return {"error": "FORBIDDEN", "message": "仅管理员可用"}

    session = SessionLocal()
    try:
        if action == "force_release":
            if not lease_id:
                return {"error": "MISSING_FIELD", "message": "force_release 需要 lease_id"}
            return leasing.force_release(session, auth, lease_id, reason or "管理员强制回收")
        if action == "overview":
            from app.constants import is_claimable

            total = session.query(Device).count()
            rows = session.query(Device).all()
            available = sum(
                1 for d in rows
                if is_claimable(d.status, d.health_level, settings.allow_apply_when_suspect)
            )
            suspect = sum(1 for d in rows if d.status == "available") - available
            occupied = session.query(Device).filter(Device.status == "occupied").count()
            offline = session.query(Device).filter(Device.status == "offline").count()
            active_leases = session.query(Lease).filter(Lease.state.in_(["GRANTED", "EXPIRING", "RESERVED"])).count()
            return {
                "total_devices": total,
                "available": available,
                "suspect": suspect,
                "occupied": occupied,
                "offline": offline,
                "active_leases": active_leases,
            }
        if action == "stats":
            from app.constants import QueueState, is_claimable

            rows = session.query(Device).all()
            available = sum(
                1 for d in rows
                if is_claimable(d.status, d.health_level, settings.allow_apply_when_suspect)
            )
            suspect = sum(1 for d in rows if d.status == "available") - available
            occupied = sum(1 for d in rows if d.status == "occupied")
            offline = sum(1 for d in rows if d.status == "offline")
            pending = sum(1 for d in rows if d.status == "pending")
            active_leases = session.query(Lease).filter(Lease.state.in_(["GRANTED", "EXPIRING", "RESERVED"])).count()
            expiring = session.query(Lease).filter(Lease.state == "EXPIRING").count()
            reserved = session.query(Lease).filter(Lease.state == "RESERVED").count()
            queued = session.query(QueueEntry).filter(QueueEntry.state == QueueState.QUEUED).count()
            offered = session.query(QueueEntry).filter(QueueEntry.state == QueueState.OFFERED).count()
            granted_q = session.query(QueueEntry).filter(QueueEntry.state == QueueState.GRANTED).count()
            notices = session.query(Notice).count()
            return {
                "devices": {
                    "total": len(rows),
                    "available": available,
                    "suspect": suspect,
                    "occupied": occupied,
                    "offline": offline,
                    "pending": pending,
                },
                "leases": {
                    "active": active_leases,
                    "expiring": expiring,
                    "reserved": reserved,
                },
                "queue": {
                    "queued": queued,
                    "offered": offered,
                    "granted": granted_q,
                },
                "notices": {"total": notices},
            }
        if action == "list_credentials":
            creds = session.query(Credential).order_by(Credential.id).all()
            return [
                {
                    "id": c.id,
                    "email": c.principal.email if c.principal else None,
                    "token_prefix": c.token_prefix,
                    "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                    "revoked": c.revoked,
                    "agent_label": c.agent_label,
                }
                for c in creds
            ]
        return {"error": "UNKNOWN_ACTION", "message": f"未知 action: {action}"}
    except DomainError as e:
        return _err(e)
    finally:
        session.close()


def build_mcp_app():
    """返回挂载用的 MCP streamable HTTP ASGI 应用（带鉴权中间件）。"""
    from starlette.middleware import Middleware

    from app.mcp.middleware import McpAuthMiddleware

    return mcp.http_app(
        transport="streamable-http",
        path="/",
        stateless_http=True,
        middleware=[Middleware(McpAuthMiddleware)],
    )
