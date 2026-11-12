"""MCP Server：quamm-tres（31 个工具，streamable HTTP，单实例）。

鉴权由 McpAuthMiddleware 完成；工具内 require_auth() 取当前身份。
res_admin 仅管理员可用（运行时校验），普通 token 调用返回 FORBIDDEN。
"""
from __future__ import annotations

import logging

from fastmcp import FastMCP

from app.config import settings
from app.runtime_config import get_runtime_value
from app.constants import AGENT_CONTRACT_VERSION, AGENT_REQUIRED_FIELDS, get_version
from app.db import SessionLocal
from app.domain import catalog, credentials, feedback, flash, leasing
from app.domain.catalog import DomainError
from app import db_source, screenshot
from app.models import Credential, Device, Lease, Notice, PatrolRun, Principal, QueueEntry
from app.security import AuthError, require_auth
from app.domain import build_servers as buildsvc
from app import sdk_build
from app.roadmap import list_roadmap
from app.utils import utcnow

mcp = FastMCP(
    "quamm-tres",
    instructions=(
        "测试设备资源分配中心。先 res_apply 拿租约和 adb_address，再 adb connect，"
        "用完 res_release。长任务到期前调 res_renew 续约。"
    ),
)

logger = logging.getLogger("quamm-tres.mcp")


def _err(e: DomainError) -> dict:
    return {"error": e.code, "message": e.message, **(e.detail or {})}


def _guard(fn, *args, **kwargs):
    """带身份调用领域函数（session + auth）。"""
    try:
        auth = require_auth()
        session = SessionLocal()
        try:
            # 中间件解析身份后已关闭其会话，credential/principal 处于游离态；
            # 合并到当前会话，否则对 agent_label 等字段的修改不会被提交。
            auth.credential = session.merge(auth.credential)
            auth.principal = session.merge(auth.principal)
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
        _serial_list_devices,
        model_class=model_class,
        status=status,
        limit=limit,
        claimable_only=claimable_only,
    )


@mcp.tool()
def res_list_positions() -> list[dict]:
    """查询所有已注册设备的位置清单（含 IP / ADB 地址 / 责任人 / 状态）。

    位置为描述性文本（工位/会议室/机架机位等），按位置排序；
    用于按物理位置找设备，或核对哪些设备尚未登记位置。
    """
    return _guard_ro(catalog.list_positions)


@mcp.tool()
def res_device_info(device_id: str) -> dict:
    """单台设备详情：机型、位置、状态、健康级别、当前占用（脱敏）+ 串口能力。"""
    return _guard_ro(_serial_device_info, device_id)


@mcp.tool()
def res_my_leases() -> list[dict]:
    """我当前持有的所有活跃租约（含 adb_address 与剩余时间）。"""
    return _guard(leasing.my_leases)


# ---------- 申请 / 续约 / 释放 ----------

@mcp.tool()
def res_apply(
    agent_info: str,
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
    - agent_info（必填）：调用方 Agent 名称/版本，如 "WorkBuddy/1.2"；缺失会被拒绝。

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
        agent_info=agent_info,
    )


@mcp.tool()
def res_renew(lease_id: str, extra_minutes: int = 30) -> dict:
    """续约：延长租约到期时间。宽限期内续约可恢复 ACTIVE。"""
    return _guard(leasing.renew, lease_id, extra_minutes=extra_minutes)


@mcp.tool()
def res_heartbeat(lease_id: str, agent_info: str | None = None) -> dict:
    """心跳（v0.18，强制）：确认设备仍在使用，同时刷新 TTL。

    应按 res_health 返回的 heartbeat_interval 周期调用；超时未心跳会被 worker 强制回收。
    agent_info 可带调用方身份（如 WorkBuddy/豆包，可带版本），回写到凭据。
    """
    result = _guard(leasing.heartbeat, lease_id, agent_info=agent_info)
    if isinstance(result, dict) and not result.get("error"):
        result["contract_version"] = AGENT_CONTRACT_VERSION
    return result


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
            "contract_version": AGENT_CONTRACT_VERSION,
            "required_fields": list(AGENT_REQUIRED_FIELDS),
            "heartbeat": {
                "required": bool(get_runtime_value(session, "heartbeat_required")),
                "interval_minutes": int(get_runtime_value(session, "heartbeat_interval_minutes")),
                "miss_grace_minutes": int(get_runtime_value(session, "heartbeat_miss_grace_minutes")),
            },
            "device_lock_path": settings.device_lock_path,
            "last_patrol_at": last.ended_at.isoformat() if last and last.ended_at else None,
            "data_age_seconds": fresh,
        }
    finally:
        session.close()


@mcp.tool()
def res_roadmap(include_released: bool = False, include_pending: bool = True) -> dict:
    """查看路线图与后续逐步释放的能力（todo/backlog）。

    - 默认返回待发布/进行中项，并附带待拍板项；
    - include_released=true 时同时列出已发布能力；
    - include_pending=false 时隐藏待拍板项。
    每项含 id、标题、描述、状态（planned/in_progress/pending_decision/released）、类别与计划版本。
    """
    return list_roadmap(include_released=include_released, include_pending=include_pending)


@mcp.tool()
def res_feedback(category: str, content: str) -> dict:
    """提交对本系统的意见/建议，供管理员批判性吸收采纳。

    - category: bug（缺陷）/improvement（改进建议）/question（疑问）/praise（肯定）；
    - content: 具体描述，建议包含场景、期望行为与复现方式，不超过 4000 字；
    - 提交后管理员会在设置「Agent 意见」中评审（采纳/暂缓/驳回）。
    """
    return _guard(
        lambda s, auth: feedback.submit_feedback(
            s, credential=auth.credential, category=category, content=content
        )
    )


# ---------- 设备注册（B 域） ----------

@mcp.tool()
def res_device_probe(
    ip: str,
    port: int = 5555,
    hint_model: str | None = None,
    hint_position: str | None = None,
    agent_info: str | None = None,
    source: str | None = None,
    timeout: int | None = None,
) -> dict:
    """探测设备 IP 并出注册表（两段式第一步）。返回 form_id，30 分钟有效。

    agent_info：调用方 Agent 自报身份（如 WorkBuddy/豆包，可带版本），作为备注随表单暂存。
    """
    return _guard_ro(
        catalog.device_probe,
        ip,
        port=port,
        hint_model=hint_model,
        hint_position=hint_position,
        agent_info=agent_info,
        source=source,
        timeout=timeout,
    )


@mcp.tool()
def res_device_register(
    agent_info: str,
    form_id: str,
    position_code: str,
    owner: str,
    model_class: str | None = None,
    note: str | None = None,
    share_scope: str | None = None,
    allowed_emails: list[str] | None = None,
    allowed_agent_labels: list[str] | None = None,
) -> dict:
    """提交注册表（两段式第二步，agent_info 为必填）。位置编号与责任人缺失会拒绝。

    可选共享设置：share_scope=shared（默认公共）/private（个人独享）/restricted（指定名单）；
    restricted 时用 allowed_emails（用户邮箱）与 allowed_agent_labels（Agent 名）给出允许名单。
    

    agent_info（必填）：调用方 Agent 名称/版本（如 WorkBuddy/1.2、豆包/1.0），写入审计并
    回写到当前凭据的 agent_label，便于在凭据页识别该用户使用的 Agent。
    """
    auth = require_auth()
    session = SessionLocal()
    try:
        # 合并游离凭据，保证 agent_label 回写能被提交。
        auth.credential = session.merge(auth.credential)
        result = catalog.device_register(
            session, form_id, position_code, owner,
            actor_email=auth.email, model_class=model_class, note=note, agent_info=agent_info,
            share_scope=share_scope, allowed_emails=allowed_emails,
            allowed_agent_labels=allowed_agent_labels,
        )
        # 用解析后的 agent_info（可能回退自 probe 暂存）回写凭据，避免显式入参为空时漏写。
        resolved_label = str(result.get("agent_info") or "").strip()[:128] or None
        if resolved_label and auth.credential.agent_label != resolved_label:
            auth.credential.agent_label = resolved_label
            session.commit()
        return result
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
def res_device_update(
    device_id: str,
    changes: dict,
) -> dict:
    """编辑设备（管理员可改全部；注册者可改 display_name/owner/position_code/共享策略）。

    changes 为字段字典，常用键：share_scope（shared/private/restricted）、
    allowed_emails（邮箱数组）、allowed_agent_labels（Agent 名数组）、display_name、
    owner、position_code；管理员还可改 model_class/ip/adb_port/mac。
    锁定：locked=true/false（可配 locked_reason），锁定后不参与任何分配（管理员或注册者可操作）。
    非注册人且非管理员返回 FORBIDDEN。
    """
    auth = require_auth()
    session = SessionLocal()
    try:
        return catalog.device_update(
            session, device_id, actor_email=auth.email,
            is_admin=auth.is_admin, changes=changes,
        )
    except DomainError as e:
        return _err(e)
    finally:
        session.close()


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


# ---------- DB 构建源（吸纳 dbxostest） ----------

def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except DomainError as e:
        return _err(e)


def _resolve_addr(session, device_id: str | None, ip: str | None, port: int | None) -> tuple[str, int]:
    if device_id:
        dev = session.get(Device, device_id)
        if not dev:
            raise DomainError("DEVICE_NOT_FOUND", f"设备不存在：{device_id}")
        return dev.ip, (dev.adb_port or 5555)
    if not ip:
        raise DomainError("MISSING_TARGET", "需提供 device_id 或 ip")
    return ip, (port or 5555)


@mcp.tool()
def res_db_probe(source: str = "daily", project_path: str | None = None) -> dict:
    """探测 DB 目录是否可用并智能适配（source 用 daily/release 或完整 URL；project_path 可多级）。

    返回层级类型 group（源根）/project（含数字版本）/version（含包）、子目录、版本、包清单。
    """
    return _call(db_source.probe_directory, source, project_path)


@mcp.tool()
def res_db_versions(source: str = "daily", project_path: str = "",
                    version: int | None = None) -> dict:
    """发现工程可用 DB 版本：指定版本优先，其次 latest/，再 HTML 解析，二分兜底；并列出包。"""
    def run():
        norm = db_source.normalize_source(source, project_path)
        if not norm["project_url"]:
            raise DomainError("MISSING_PROJECT", "需提供 project_path")
        fv = db_source.find_versions(norm["project_url"], version)
        fv["packages"] = db_source.list_packages(fv["version_url"])
        return fv
    return _call(run)


@mcp.tool()
def res_db_device_version(device_id: str | None = None, ip: str | None = None,
                          port: int = 5555, project: str | None = None) -> dict:
    """读取设备当前 DB 版本（adb shell /.DB_*），可按 device_id 或 ip:port 定位。"""
    try:
        session = SessionLocal()
        try:
            addr, p = _resolve_addr(session, device_id, ip, port)
            return db_source.device_db_version(addr, p, project)
        finally:
            session.close()
    except DomainError as e:
        return _err(e)


@mcp.tool()
def res_db_prepare(source: str = "daily", project_path: str = "",
                   version: int | None = None) -> dict:
    """下载并解压指定 DB 版本，返回本地包路径与 qmimages/script.ini（下载即落盘，需有效凭据）。

    v0.28.3：下载前先按保留策略回收旧缓存（过期解压目录、每个 label 超份数的旧包），
    返回的路径仍可用，但会被后续回收按 db_source_keep_days 清掉，别当长期存储。
    """
    try:
        require_auth()
        norm = db_source.normalize_source(source, project_path)
        if not norm["project_url"]:
            raise DomainError("MISSING_PROJECT", "需提供 project_path")
        cleaned = db_source.prune_work_root()
        fv = db_source.find_versions(norm["project_url"], version)
        pkg = db_source.list_packages(fv["version_url"])[0]
        label = project_path.replace("/", "_") + f"_{fv['selected']}"
        dl = db_source.download_package(pkg["url"], label)
        ex = db_source.extract_and_find_ini(dl["package"])
        return {"version": fv["selected"], "strategy": fv["strategy"], **dl, **ex,
                "cache_pruned": cleaned}
    except DomainError as e:
        return _err(e)
    except AuthError as e:
        return {"error": "AUTH", "message": e.message}


@mcp.tool()
def res_db_upgrade(device_id: str | None = None, ip: str | None = None, port: int = 5555,
                   source: str = "daily", project_path: str = "",
                   version: int | None = None,
                   update_args: str = "-f -a -e data") -> dict:
    """端到端 DB 升级：发现版本→下载 CURRENT*.gz→解压定位 script.ini→xosadb2update。

    需有效凭据；同步执行（数分钟），默认保留 data（-f -a -e data）。可指定 device_id 或 ip。
    v0.28.3：升级结束（无论成败）立即删掉本次下载的包与解压目录，不留在 var/db_source。
    """
    try:
        require_auth()
        session = SessionLocal()
        try:
            addr, _ = _resolve_addr(session, device_id, ip, port)
        finally:
            session.close()
        norm = db_source.normalize_source(source, project_path)
        if not norm["project_url"]:
            raise DomainError("MISSING_PROJECT", "需提供 project_path")
        before = db_source.device_db_version(addr, port, project_path or None)
        cleaned = db_source.prune_work_root()
        fv = db_source.find_versions(norm["project_url"], version)
        pkg = db_source.list_packages(fv["version_url"])[0]
        label = project_path.replace("/", "_") + f"_{fv['selected']}"
        dl = db_source.download_package(pkg["url"], label)
        ex = db_source.extract_and_find_ini(dl["package"])
        try:
            up = db_source.upgrade(addr, ex["script_ini"], update_args)
            after = db_source.device_db_version(addr, port, project_path or None)
            return {"before": before, "target_version": fv["selected"], "package": dl["name"],
                    "upgrade": up, "after": after, "cache_pruned": cleaned}
        finally:
            # 刷完即清：包与解压目录都只服务一次升级，留着纯占空间
            discarded = db_source.discard_work(dl.get("package"), ex.get("extract_dir"))
            if discarded["removed"]:
                logger.info("db upgrade cleanup: %s", discarded)
    except DomainError as e:
        return _err(e)
    except AuthError as e:
        return {"error": "AUTH", "message": e.message}


# ---------- 设备截图（v0.23） ----------

@mcp.tool()
def res_screenshot(
    device_id: str | None = None,
    ip: str | None = None,
    port: int = 5555,
    quality: int = 60,
    rotate: int = 0,
) -> dict:
    """对指定设备截图并保存到服务器，返回文件路径与元数据。

    自动按浮点 ABI 选设备端二进制：hard=vxosdbg（带 VFP）/ soft=xosdbg（无 FPU），
    优先取机型配置的 float_abi/has_fpu，否则读 /proc/cpuinfo 的 Features，再兜底运行时探测。
    device_id（从库读 ip/port）或直接 ip/port 二选一；quality 1-100（默认 60），
    rotate 0/90/180/270（默认 0）。返回 file_name 可用 REST GET /api/screenshots/{file_name} 读取。
    需有效凭据（require_auth）；失败返回 {"error": code, "message": ...}。
    """
    def run(session, auth):
        return screenshot.capture_screenshot(
            device_id=device_id,
            ip=ip,
            port=port,
            quality=quality,
            rotate=rotate,
            session=session,
            created_by=(auth.credential.agent_label or auth.email),
        )

    return _guard(run)


# ---------- 管理 ----------

@mcp.tool()
def res_admin(
    action: str,
    lease_id: str | None = None,
    reason: str | None = None,
    device_id: str | None = None,
    email: str | None = None,
    role: str = "user",
    name: str | None = None,
    note: str | None = None,
    valid_days: int | None = None,
    credential_id: int | None = None,
    confirm: bool = False,
    send: bool = False,
) -> dict:
    """管理员工具（普通 token 不可用）。

    action 枚举：
    - force_release：强制回收租约（需 lease_id，reason 必填）
    - overview：设备池概览（total/available/suspect/occupied/offline/active_leases）
    - stats：汇总统计（设备/租约/队列/通知四组计数）
    - list_credentials：列出全部凭据（token 尾号/有效期/是否吊销）
    - invite：给指定邮箱发邀请函/凭据（email 必填；role=user/admin；send=true 直接 SMTP 发信，
      否则只生成返回 token；已有有效凭据时返回 needs_confirm，confirm=true 才补发）
    - revoke_credential：吊销凭据并级联强制回收其活跃租约（需 credential_id）
    - restore_credential：恢复已吊销凭据（需 credential_id）
    - delete_credential：硬删除凭据（需 credential_id；有租约引用则拒绝）
    - force_delete_credential：管理员强制删除（需 credential_id；级联回收并删除关联租约/排队数据）
    """
    auth = require_auth()
    if not auth.is_admin:
        return {"error": "FORBIDDEN", "message": "仅管理员可用"}

    session = SessionLocal()
    try:
        if action == "invite":
            if not email:
                return {"error": "MISSING_FIELD", "message": "invite 需要 email"}
            return credentials.issue_credential(
                session,
                email,
                role=role,
                name=name,
                note=note,
                valid_days=valid_days,
                actor_email=auth.email,
                confirm=confirm,
                send=send,
            )
        if action == "force_release":
            if not lease_id:
                return {"error": "MISSING_FIELD", "message": "force_release 需要 lease_id"}
            return leasing.force_release(session, auth, lease_id, reason or "管理员强制回收")
        if action == "revoke_credential":
            if credential_id is None:
                return {"error": "MISSING_FIELD", "message": "revoke_credential 需要 credential_id"}
            return credentials.revoke_credential(
                session, credential_id, actor=auth.email, reason=reason or "管理员吊销"
            )
        if action == "restore_credential":
            if credential_id is None:
                return {"error": "MISSING_FIELD", "message": "restore_credential 需要 credential_id"}
            return credentials.restore_credential(session, credential_id, actor=auth.email)
        if action == "delete_credential":
            if credential_id is None:
                return {"error": "MISSING_FIELD", "message": "delete_credential 需要 credential_id"}
            return credentials.delete_credential(session, credential_id, actor=auth.email)
        if action == "force_delete_credential":
            if credential_id is None:
                return {"error": "MISSING_FIELD", "message": "force_delete_credential 需要 credential_id"}
            return credentials.force_delete_credential(session, credential_id, actor=auth.email)
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


@mcp.tool()
def res_build_servers(
    action: str,
    server_id: str | None = None,
    name: str | None = None,
    host: str | None = None,
    port: int | None = None,
    username: str | None = None,
    auth_mode: str | None = None,
    secret: str | None = None,
    work_root: str | None = None,
    subdir: str | None = None,
    include_all: bool = False,
) -> dict:
    """编译服务器自助管理（任何有效凭据；默认只看/只操作本账号自己的服务器）。

    密钥 Fernet 加密、只回显掩码，服务端集中执行、不下发明文。一账号一台。
    action: list / create / update / delete / probe
    - create：name/host/username 必填，auth_mode=password|key（key 留空 secret 自动生成 ed25519）
    - probe：逐步 tcp→auth→workdir→git→toolchain，决定当前账号的 sdk.build_upgrade 能力
    - list：默认严格按账号隔离；管理员排障可传 include_all=true 查看全部
    """
    auth = require_auth()
    principal_id = auth.principal.id
    is_admin = bool(auth.is_admin)
    session = SessionLocal()
    try:
        if action == "list":
            return {"items": buildsvc.list_build_servers(
                session, principal_id=principal_id, is_admin=is_admin,
                include_all=include_all)}
        if action == "create":
            return buildsvc.create_build_server(
                session, actor=auth.email, principal_id=principal_id,
                name=name or "", host=host or "", port=int(port or 22),
                username=username or "", auth_mode=auth_mode or "password",
                secret=secret or None, work_root=work_root or "tres-auto",
            )
        if action == "update":
            if not server_id:
                return {"error": "MISSING_FIELD", "message": "update 需要 server_id"}
            return buildsvc.update_build_server(
                session, actor=auth.email, principal_id=principal_id, is_admin=is_admin,
                server_id=server_id, name=name, host=host, port=port, username=username,
                auth_mode=auth_mode, secret=secret or None, work_root=work_root,
            )
        if action == "delete":
            if not server_id:
                return {"error": "MISSING_FIELD", "message": "delete 需要 server_id"}
            return buildsvc.delete_build_server(
                session, actor=auth.email, principal_id=principal_id,
                is_admin=is_admin, server_id=server_id,
            )
        if action == "probe":
            if not server_id:
                return {"error": "MISSING_FIELD", "message": "probe 需要 server_id"}
            return buildsvc.probe_build_server(
                session, principal_id=principal_id, is_admin=is_admin,
                server_id=server_id, subdir=subdir,
            )
        return {"error": "UNKNOWN_ACTION", "message": f"未知 action: {action}"}
    except DomainError as e:
        return _err(e)
    finally:
        session.close()


@mcp.tool()
def res_sdk_build_upgrade(
    type_code: str,
    ip_suffix: str | None = None,
    timeout: int = 1800,
) -> dict:
    """在编译机拉最新代码并编译（服务端 SSH 集中执行，agent 不接触密钥）。

    工作目录 ~/<work_root>/<机型>-<IP尾号>；当前账号需已配置自己的编译服务器且机型配置
    sdk_repo_cmd / build_cmd。返回各步 rc 与日志，success 表示编译通过。
    """
    try:
        auth = require_auth()
        session = SessionLocal()
        try:
            return sdk_build.run_sdk_build(
                session, principal_id=auth.principal.id, type_code=type_code,
                ip_suffix=ip_suffix, timeout=timeout,
            )
        finally:
            session.close()
    except DomainError as e:
        return _err(e)
    except AuthError as e:
        return {"error": "AUTH", "message": e.message}


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


# ---------------- v0.29 设备层串口 ----------------
from app.domain import serial as serial_svc


def _serial_actor(auth) -> str:
    label = getattr(getattr(auth, "credential", None), "agent_label", None)
    return f"{label} ({auth.email})" if label else auth.email


def _serial_list_devices(
    session,
    model_class=None,
    status=None,
    limit=100,
    claimable_only=True,
):
    rows = catalog.list_devices(
        session,
        model_class=model_class,
        status=status,
        limit=limit,
        for_agent=True,
        claimable_only=claimable_only,
    )
    flags = serial_svc.serial_flags(session)
    for r in rows:
        r["serial"] = flags.get(
            r.get("device_id"), {"log": False, "power": False, "mode": False, "slots": 0}
        )
    return rows


def _serial_device_info(session, device_id):
    info = catalog.device_info(session, device_id, for_agent=True)
    try:
        info["serial"] = serial_svc.serial_summary(session, device_id)
    except DomainError:
        info["serial"] = {"available": False}
    return info


def _serial_action_impl(session, auth, device_id, action_key, lease_id=None,
                        confirm=False, hold_ms=None, wait_ms=None):
    serial_svc.check_access(
        session, device_id,
        email=auth.email, is_admin=auth.is_admin,
        action_key=action_key, confirm=confirm,
    )
    return serial_svc.run_action(
        session, device_id, action_key,
        by=_serial_actor(auth), lease_id=lease_id, hold_ms=hold_ms,
        wait_ms=wait_ms if wait_ms is not None else serial_svc.DEFAULT_HOLD_WAIT_MS,
    )


def _serial_release_impl(session, auth, device_id, slot=None):
    serial_svc.check_access(session, device_id, email=auth.email, is_admin=auth.is_admin)
    return serial_svc.release_device_ports(
        session, device_id, actor=_serial_actor(auth), slot=slot
    )


def _serial_read_impl(session, auth, device_id, slot=None, seconds=3.0):
    serial_svc.check_access(session, device_id, email=auth.email, is_admin=auth.is_admin)
    return serial_svc.read_serial(
        session, device_id, slot=slot, seconds=seconds, by=_serial_actor(auth)
    )


def _serial_probe_impl(session, auth, device_id, slot=None):
    serial_svc.check_access(session, device_id, email=auth.email, is_admin=auth.is_admin)
    return serial_svc.probe_port(session, device_id, slot=slot, by=_serial_actor(auth))


def _serial_config_impl(session, auth, device_id, ports, replace=False):
    if not auth.is_admin:
        raise DomainError("FORBIDDEN", "串口配置只允许管理员")
    return serial_svc.upsert_ports(
        session, device_id, ports, actor=auth.email, replace=replace
    )


def _serial_report_impl(session, auth, device_id, action_key, result, detail=None):
    return serial_svc.report_result(
        session, device_id, action_key, result, by=_serial_actor(auth), detail=detail
    )


@mcp.tool()
def res_serial_caps(device_id: str) -> dict:
    """查一台设备的串口能力：端口、动作（含 hex 序列）、电源/模式状态、当前谁占着。"""
    return _guard_ro(serial_svc.caps, device_id)


@mcp.tool()
def res_serial_action(
    device_id: str,
    action_key: str,
    lease_id: str | None = None,
    confirm: bool = False,
    hold_ms: int | None = None,
    wait_ms: int | None = None,
) -> dict:
    """服务端执行一个已定义串口动作（power_on / power_off / power_cycle / mode_* / custom_*）。

    需要持有该设备租约或管理员权限；mode_edl 额外要 confirm=true。
    power_cycle 若没单独定义，会按 power_off + 断电保持(默认1200ms) + power_on 组合执行。
    """
    return _guard(_serial_action_impl, device_id, action_key, lease_id, confirm, hold_ms, wait_ms)


@mcp.tool()
def res_serial_read(device_id: str, slot: int | None = None, seconds: float = 3.0) -> dict:
    """短读串口（默认 log 口），返回这几秒收到的原始文本。"""
    return _guard(_serial_read_impl, device_id, slot, seconds)


@mcp.tool()
def res_serial_probe(device_id: str, slot: int | None = None) -> dict:
    """自检串口：能不能连上、有没有回包字节（配错波特率/端口时先跑这个）。"""
    return _guard(_serial_probe_impl, device_id, slot)


@mcp.tool()
def res_serial_config(device_id: str, ports: list[dict], replace: bool = False) -> dict:
    """配置设备串口与动作（仅管理员）。ports 结构见 docs §3。"""
    return _guard(_serial_config_impl, device_id, ports, replace)


@mcp.tool()
def res_serial_report(device_id: str, action_key: str, result: str,
                      detail: dict | None = None) -> dict:
    """skill 直连 TCP 执行后回填 TRES 状态与审计（要求已认证身份，且动作须已定义）。"""
    return _guard(_serial_report_impl, device_id, action_key, result, detail)


@mcp.tool()
def res_serial_log(device_id: str, since: int = 0, max_bytes: int = 8192,
                   keyword: str | None = None) -> dict:
    """读 tres-logs 侧 log 增量。tres-logs 的日志接口（规划 C4）尚未对齐，当前返回不可用。"""
    try:
        c = serial_svc.caps(SessionLocal(), device_id)
    except DomainError:
        c = {"log": {"available": False}}
    return {
        "device_id": device_id,
        "available": False,
        "reason": "tres-logs 的 log 接口（C4）尚未对齐，暂用 res_serial_read 短读",
        "log": c.get("log"),
    }


@mcp.tool()
def res_serial_release(device_id: str, slot: int | None = None) -> dict:
    """释放该设备占用的物理串口。

    一路串口挂多台设备（共享电源口）时，控制完必须放，否则下一台会 busy；
    Agent 异常中断后也用它兜底，不用等 ttl（默认 120s）。
    """
    return _guard(_serial_release_impl, device_id, slot)


@mcp.tool()
def res_serial_token(lease_id: str) -> dict:
    """拿本租约的日志机串口「派生票」（serialhub 1.1.0，受限票）。

    返回 `token`（rt_ 开头）、日志机 access（raw_tcp / mcp_url / slots）、有效期与可用工具范围。
    - 直连方式：raw TCP 连上收到 `login: ` 后发 `<token>` + 换行(LF)；或 MCP
      `Authorization: Bearer <token>` 调 `http://<日志机>:8787/mcp`。
    - 只读 + 本路串口读写；管理面工具（改波特率/开关口/标定/巡检/发工单）会被 serialhub 拒绝。
    - **每次调用都会重签，旧票立即作废**（只保留最新一张）；租约释放/超时回收后立即失效。
    - 主 token 永不下发；拿不到票说明日志机不可达，会返回 SERIAL_TOKEN_PENDING，稍后重试即可。
    """
    from app.domain import serial_token

    return _guard(serial_token.token_for_lease, lease_id)


# ---------------- WireLog 日志机域（见 WIRELOG-DESIGN.md）

from app.domain import wirelog as wl_svc


@mcp.tool()
def wl_list_nodes() -> list[dict]:
    """列出所有日志机（serialhub）：状态、IP、路数、最近心跳。"""
    return _guard_ro(wl_svc.list_nodes)


@mcp.tool()
def wl_node_info(node_id: str) -> dict:
    """日志机详情：能力、配置、各路串口绑定与 shell 就绪状态。"""
    return _guard_ro(wl_svc.get_node, node_id)


def _wl_enroll_impl(session, auth, adb_address=None, display_name=None,
                    model_class="esl2_hk"):
    if not auth.is_admin:
        raise DomainError("FORBIDDEN", "日志机发放只允许管理员")
    return wl_svc.enroll(session, adb_address=adb_address, operator=auth.email,
                         display_name=display_name, model_class=model_class)


@mcp.tool()
def wl_enroll(adb_address: str | None = None, display_name: str | None = None,
              model_class: str = "esl2_hk") -> dict:
    """发放日志机：生成 node token 与 fw_setenv 命令（仅管理员，明文只回一次）。"""
    return _guard(_wl_enroll_impl, adb_address, display_name, model_class)


def _wl_config_impl(session, auth, node_id, config):
    if not auth.is_admin:
        raise DomainError("FORBIDDEN", "配置下发只允许管理员")
    return wl_svc.set_config(session, node_id, config, auth.email)


@mcp.tool()
def wl_set_config(node_id: str, config: dict) -> dict:
    """下发日志机配置（raw tcp、本地 token 轮换、端口参数等），仅管理员。"""
    return _guard(_wl_config_impl, node_id, config)


def _wl_bind_impl(session, auth, node_id, slot, device_id=None):
    return wl_svc.bind_port(session, node_id, slot, device_id, auth.email)


@mcp.tool()
def wl_bind_port(node_id: str, slot: int,
                 device_id: str | None = None) -> dict:
    """绑定某路串口到设备（按 MAC 自动匹配或指定 device_id）。"""
    return _guard(_wl_bind_impl, node_id, slot, device_id)


def _wl_grant_impl(session, auth, node_id, slot, principal):
    if not auth.is_admin:
        raise DomainError("FORBIDDEN", "写授权只允许管理员")
    return wl_svc.grant_write(session, node_id, slot, principal, auth.email)


@mcp.tool()
def wl_grant_write(node_id: str, slot: int, principal: str) -> dict:
    """授予某路串口写权限给指定 principal（仅管理员）。"""
    return _guard(_wl_grant_impl, node_id, slot, principal)


def _wl_revoke_impl(session, auth, node_id, slot):
    if not auth.is_admin:
        raise DomainError("FORBIDDEN", "撤销写授权只允许管理员")
    return wl_svc.revoke_write(session, node_id, slot, auth.email)


@mcp.tool()
def wl_revoke_write(node_id: str, slot: int) -> dict:
    """撤销某路串口写权限（仅管理员），撤销后连接回只读。"""
    return _guard(_wl_revoke_impl, node_id, slot)


@mcp.tool()
def wl_audit_tail(node_id: str | None = None, limit: int = 50) -> list[dict]:
    """查 WireLog 审计（注册/心跳/配置/授权/绑定）。"""
    return _guard_ro(wl_svc.audit_tail, node_id, limit)


def _wl_uart_register_impl(session, auth, node_id, slot, position_code=None,
                            owner=None):
    return wl_svc.uart_register(session, node_id, slot, auth.email,
                                position_code, owner)


@mcp.tool()
def wl_uart_register(node_id: str, slot: int,
                     position_code: str | None = None,
                     owner: str | None = None) -> dict:
    """经 WireLog 串口全量探测并注册/更新设备（MAC/SN 二选一）。"""
    return _guard(_wl_uart_register_impl, node_id, slot, position_code, owner)


def _wl_reconcile_impl(session, auth, node_id, slot, observed):
    return wl_svc.reconcile_identity(session, node_id, slot, observed,
                                     auth.email)


@mcp.tool()
def wl_reconcile_identity(node_id: str, slot: int, observed: dict) -> dict:
    """全量探测后按 MAC/SN 锚定，处理 IP 漂移与物理串口换位。"""
    return _guard(_wl_reconcile_impl, node_id, slot, observed)


@mcp.tool()
def wl_merge_mac_conflicts() -> dict:
    """同 MAC 多设备归并：按是否有活动租约决定删/留（仅管理员）。"""
    def _impl(session, auth):
        return wl_svc.merge_mac_conflicts(session, auth.email)
    return _guard(_impl)
