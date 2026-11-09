"""资源目录与设备注册（见 10 B 域、04 身份识别）。

probe -> register 两段式，中间用 form_id 串起来（form 存内存，30 分钟有效）。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from app.config import settings
from app.constants import DeviceStatus, HealthLevel, LeaseState, QueueState, RegisterChannel, is_claimable
from app.domain import audit
from app.domain.notices import notify_device_unregistered
from app.domain.probe import probe_device
from app.models import Device, Lease, ProductType, QueueEntry
from app.runtime_config import get_runtime_value
from app.utils import new_device_id, new_flash_id, new_form_id, utcnow

FORM_TTL_MINUTES = 30

# v0.12：四列都非空才算「完整」，缺任一即「信息不全」（不强制填全）。
COMPLETE_FIELDS = ("defconfig", "sdk_repo_cmd", "build_cmd", "image_path")
METADATA_FIELDS = (
    "defconfig", "sdk_repo_cmd", "build_cmd", "image_path", "jenkins_url", "notes",
)


def product_complete(pt: ProductType) -> bool:
    """工程配置/SDK 获取/编译命令/镜像路径四列都非空 = complete。"""
    return all((getattr(pt, f, None) or "").strip() for f in COMPLETE_FIELDS)


class DomainError(Exception):
    """领域错误，code 对客户端可见。"""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


@dataclass
class _RegistrationForm:
    form_id: str
    ip: str
    port: int
    probe: dict
    created_at: object
    expires_at: object
    hint_model: str | None = None
    hint_position: str | None = None
    agent_info: str | None = None
    flash: dict = field(default_factory=dict)


def _clean_agent_info(value) -> str | None:
    """调用方 Agent 自报身份（如 WorkBuddy/豆包），作为备注；截断到 128 字符。"""
    if value is None:
        return None
    text = str(value).strip()
    return text[:128] or None


class FormStore:
    """注册表单暂存（内存 + TTL）。单进程 MCP 下够用，未来可换表/Redis。"""

    def __init__(self, ttl_minutes: int = FORM_TTL_MINUTES):
        self._forms: dict[str, _RegistrationForm] = {}
        self._flash: dict[str, dict] = {}
        self._ttl = timedelta(minutes=ttl_minutes)
        self._lock = threading.Lock()

    def put(self, form: _RegistrationForm) -> None:
        with self._lock:
            self._forms[form.form_id] = form

    def get(self, form_id: str) -> _RegistrationForm | None:
        with self._lock:
            form = self._forms.get(form_id)
            if form is None:
                return None
            if form.expires_at <= utcnow():
                self._forms.pop(form_id, None)
                return None
            return form

    def put_flash(self, flash_id: str, data: dict) -> None:
        with self._lock:
            self._flash[flash_id] = data

    def get_flash(self, flash_id: str) -> dict | None:
        with self._lock:
            return self._flash.get(flash_id)


form_store = FormStore()


# ---------- 只读 ----------

def list_products(session: Session, keyword: str | None = None, only_available: bool = False) -> list[dict]:
    q = session.query(ProductType)
    if keyword:
        q = q.filter(
            (ProductType.type_code.ilike(f"%{keyword}%"))
            | (ProductType.display_name.ilike(f"%{keyword}%"))
        )
    products = q.order_by(ProductType.type_code).all()

    # 每个机型统计：总数 / 可用 / 占用 / leased / queued（量级小，Python 侧计数，跨库一致）
    devices = session.query(Device).filter(Device.status != DeviceStatus.RETIRED).all()
    stats: dict[str, dict] = {
        p.type_code: {"total": 0, "available": 0, "occupied": 0, "leased": 0, "queued": 0}
        for p in products
    }
    device_model = {d.device_id: d.model_class for d in devices}
    for d in devices:
        if d.model_class not in stats:
            stats[d.model_class] = {"total": 0, "available": 0, "occupied": 0, "leased": 0, "queued": 0}
        st = stats[d.model_class]
        st["total"] += 1
        if is_claimable(
            d.status, d.health_level, get_runtime_value(session, "allow_apply_when_suspect")
        ):
            st["available"] += 1
        if d.status == DeviceStatus.OCCUPIED:
            st["occupied"] += 1

    # leased：该机型设备上处于活跃态的租约数（按机型池汇总）
    active_leases = (
        session.query(Lease).filter(Lease.state.in_(LeaseState.ACTIVE)).all()
    )
    for l in active_leases:
        model = device_model.get(l.device_id)
        if model in stats:
            stats[model]["leased"] += 1

    # queued：按机型排队（target_type=product）且仍 QUEUED 的人数
    product_queues = (
        session.query(QueueEntry)
        .filter(QueueEntry.target_type == "product", QueueEntry.state == QueueState.QUEUED)
        .all()
    )
    for e in product_queues:
        if e.target_code in stats:
            stats[e.target_code]["queued"] += 1

    result = []
    for p in products:
        st = stats.get(p.type_code, {"total": 0, "available": 0, "occupied": 0, "leased": 0, "queued": 0})
        if only_available and st["available"] <= 0:
            continue
        complete = product_complete(p)
        row = {
            "type_code": p.type_code,
            "display_name": p.display_name,
            "has_screen": p.has_screen,
            "has_led": p.has_led,
            "serial_count": p.serial_count,
            "adb_port": p.adb_port,
            "enabled": p.enabled,
            "total": st["total"],
            "available": st["available"],
            "occupied": st["occupied"],
            "leased": st["leased"],
            "queued": st["queued"],
            "complete": complete,
            "complete_label": "完整" if complete else "信息不全",
        }
        for f in METADATA_FIELDS:
            row[f] = getattr(p, f, None)
        result.append(row)
    return result


def _public_device(d: Device, session: Session, for_agent: bool = True) -> dict:
    """设备公开视图；for_agent=True 时脱敏（不给占用人身份，见 11 §7.5）。"""
    allow_suspect = bool(get_runtime_value(session, "allow_apply_when_suspect"))
    data = {
        "device_id": d.device_id,
        "display_name": d.display_name,
        "model_class": d.model_class,
        "status": d.status,
        "health_level": d.health_level,
        "claimable": is_claimable(d.status, d.health_level, allow_suspect),
        "adb_address": d.adb_address,
        "position_code": d.position_code,
        "project_name": d.project_name,
        "last_seen_at": d.last_seen_at.isoformat() if d.last_seen_at else None,
        "last_probe_at": d.last_probe_at.isoformat() if d.last_probe_at else None,
    }
    if not for_agent:
        data.update(
            {
                "owner": d.owner,
                "mac": d.mac,
                "sn": d.sn,
                "asset_tag": d.asset_tag,
                "registered_by": d.registered_by,
                "register_channel": d.register_channel,
                "android_version": d.android_version,
                "source": d.source,
                "board_dt": d.board_dt,
                "storage_type": d.storage_type,
                "has_wifi": d.has_wifi,
            }
        )
    return data


def list_devices(
    session: Session,
    model_class: str | None = None,
    status: str | None = None,
    limit: int = 100,
    for_agent: bool = True,
    claimable_only: bool = False,
) -> list[dict]:
    q = session.query(Device)
    if model_class:
        q = q.filter(Device.model_class == model_class)
    if status:
        q = q.filter(Device.status == status)
    devices = q.order_by(Device.device_id).all()
    if claimable_only:
        # 只把"当前真正可申请"的设备返回给 Agent，避免拿到连不上的机器。
        allow_suspect = bool(get_runtime_value(session, "allow_apply_when_suspect"))
        devices = [
            d for d in devices
            if is_claimable(d.status, d.health_level, allow_suspect)
        ]
    devices = devices[:limit]
    return [_public_device(d, session, for_agent=for_agent) for d in devices]


def list_positions(session: Session) -> list[dict]:
    """查询所有已注册设备的位置（含 IP/ADB 地址），按位置排序。

    位置为描述性文本（工位/会议室/机架机位等），不强制格式；
    未登记位置的设备排在最后，仍返回，便于补录。
    """
    devices = session.query(Device).all()
    rows = [
        {
            "device_id": d.device_id,
            "display_name": d.display_name,
            "model_class": d.model_class,
            "status": d.status,
            "health_level": d.health_level,
            "position_code": d.position_code,
            "owner": d.owner,
            "ip": d.ip,
            "adb_port": d.adb_port,
            "adb_address": d.adb_address,
        }
        for d in devices
    ]
    return sorted(rows, key=lambda r: (r["position_code"] is None, r["position_code"] or ""))


def device_info(session: Session, device_id: str, for_agent: bool = True) -> dict:
    d = session.get(Device, device_id)
    if d is None:
        raise DomainError("NOT_FOUND", f"device {device_id} not found")
    return _public_device(d, session, for_agent=for_agent)


# ---------- 注册 ----------

def _match_existing(session: Session, probe: dict) -> Device | None:
    """强锚比对：MAC / SN / asset_tag 任一唯一命中即认定为已有设备。"""
    for key in ("mac", "sn", "asset_tag"):
        value = probe.get(key)
        if value:
            found = session.query(Device).filter(getattr(Device, key) == value).first()
            if found:
                return found
    return None


def device_probe(
    session: Session,
    ip: str,
    port: int = 5555,
    hint_model: str | None = None,
    hint_position: str | None = None,
    source: str | None = None,
    agent_info: str | None = None,
) -> dict:
    # source 为空时沿用全局后端；给出时按来源分流（real=真实 XOS ADB）。
    result = probe_device(ip, port or 5555, timeout=settings.probe_timeout_seconds, source=source)
    probe = {
        "ip": ip,
        "adb_port": port,
        "level": result.level,
        "mac": result.mac,
        "sn": result.sn,
        "model": result.model,
        "android_version": result.android_version,
        "build_fingerprint": result.build_fingerprint,
        "asset_tag": result.asset_tag,
    }

    # 同 IP 幂等：30 分钟内重复 probe 返回同一个 form
    now = utcnow()
    existing = _match_existing(session, probe)
    match = {"result": "new", "matched_device_id": None, "matched_by": None}
    if existing is not None:
        matched_by = "mac" if probe.get("mac") and existing.mac == probe["mac"] else None
        matched_by = matched_by or ("sn" if probe.get("sn") and existing.sn == probe["sn"] else None)
        matched_by = matched_by or (
            "asset_tag" if probe.get("asset_tag") and existing.asset_tag == probe["asset_tag"] else None
        )
        match = {"result": "existing", "matched_device_id": existing.device_id, "matched_by": matched_by}

    form = _RegistrationForm(
        form_id=new_form_id(),
        ip=ip,
        port=port,
        probe=probe,
        created_at=now,
        expires_at=now + timedelta(minutes=FORM_TTL_MINUTES),
        hint_model=hint_model,
        hint_position=hint_position,
        agent_info=_clean_agent_info(agent_info),
    )
    form_store.put(form)

    hint_conflict = None
    if hint_model and result.model and hint_model != result.model:
        hint_conflict = f"探测机型 {result.model} 与提示 {hint_model} 不一致，以探测为准"

    need = ["position_code", "owner"]
    if result.reachable_l3:
        # L3 才提示点亮核对；无屏无灯机型跳过
        pass

    return {
        "form_id": form.form_id,
        "expires_at": form.expires_at.isoformat(),
        "probe": probe,
        "identity_match": match,
        "hint_conflict": hint_conflict,
        "need_from_human": need,
        "optional": ["flash_confirm"] if result.reachable_l3 else [],
        "next": "请让使用者提供位置编号（格式 302-R2-05）与维护责任人，然后调用 res_device_register 提交 form_id",
    }


def device_register(
    session: Session,
    form_id: str,
    position_code: str,
    owner: str,
    actor_email: str,
    model_class: str | None = None,
    note: str | None = None,
    agent_info: str | None = None,
) -> dict:
    form = form_store.get(form_id)
    if form is None:
        raise DomainError("FORM_EXPIRED", "表单已失效，请重新调用 res_device_probe")

    # 注册时显式自报优先；未给则回退 probe 阶段暂存的 Agent 身份。
    agent_info = _clean_agent_info(agent_info) or _clean_agent_info(
        getattr(form, "agent_info", None)
    )

    if not position_code or not owner:
        missing = []
        if not position_code:
            missing.append("position_code")
        if not owner:
            missing.append("owner")
        raise DomainError("MISSING_FIELD", "缺少必填字段：" + ", ".join(missing), {"missing": missing})

    # 位置支持描述性表达（工位/会议室/机架机位等），仅做轻量规范化，不强制格式：
    # 去首尾空白、把内部连续空白折叠为单个空格。
    position_code = " ".join(position_code.split())
    owner = " ".join(owner.split())

    probe = form.probe

    # 机型字典校验（10 §3.1：不在字典里的 model_class 拒绝，或转 unknown_model）
    final_model = model_class or probe.get("model")
    if final_model:
        pt = session.get(ProductType, final_model)
        if pt is None:
            # 探测到的机型不在字典：不阻塞，标 unknown_model（能 adb 就能用，只是按机型筛不到）
            if model_class:
                raise DomainError("UNKNOWN_MODEL", f"机型 {model_class} 不在机型字典中")
            final_model = "unknown_model"
    else:
        final_model = "unknown_model"

    # 身份强锚比对（MAC/SN/asset_tag）先算出来：
    # 重复注册同一台设备时，position_code 本来就属于它自己，不能按“位置重复”拒绝。
    existing = _match_existing(session, probe)

    # 位置唯一性：位置已被“另一台”设备占用才拒绝
    dup_pos = session.query(Device).filter(Device.position_code == position_code).first()
    if dup_pos is not None and (existing is None or dup_pos.device_id != existing.device_id):
        raise DomainError(
            "POSITION_DUP", f"位置编号 {position_code} 已分配给 {dup_pos.device_id}"
        )

    warnings: list[str] = []

    now = utcnow()
    if existing is not None:
        # 认定为已有设备：更新 IP/版本/位置，不新建（10 §4 结果 A）
        device = existing
        device.ip = probe.get("ip") or device.ip
        device.adb_port = probe.get("adb_port") or device.adb_port
        device.android_version = probe.get("android_version") or device.android_version
        device.position_code = position_code
        device.owner = owner
        if final_model != "unknown_model":
            device.model_class = final_model
        matched_as = "existing"
    else:
        # 新建（10 §4 结果 B）
        device = Device(
            device_id=new_device_id(),
            display_name=note or f"{final_model}-{position_code}",
            model_class=final_model if final_model != "unknown_model" else None,
            mac=probe.get("mac"),
            sn=probe.get("sn"),
            asset_tag=probe.get("asset_tag"),
            position_code=position_code,
            owner=owner,
            status=DeviceStatus.PENDING,
            health_level=probe.get("level", HealthLevel.L0),
            registered_by=actor_email,
            register_channel=RegisterChannel.MCP_AGENT,
            ip=probe.get("ip"),
            adb_port=probe.get("adb_port", 5555),
            android_version=probe.get("android_version"),
            build_fingerprint=probe.get("build_fingerprint"),
            last_seen_at=now if probe.get("level") == HealthLevel.L3 else None,
            created_at=now,
        )
        session.add(device)
        session.flush()
        matched_as = "new"

    # 入池门槛：L3 才 available，否则 pending（10 §5）
    if device.health_level == HealthLevel.L3:
        device.status = DeviceStatus.AVAILABLE
        device.consecutive_failures = 0
        device.last_seen_at = now
    else:
        device.status = DeviceStatus.PENDING

    device.last_probe_at = now
    audit.record(
        session,
        actor=actor_email,
        action="register",
        device_id=device.device_id,
        detail={"matched_as": matched_as, "model_class": final_model, "level": device.health_level, "agent_info": agent_info},
    )
    session.commit()

    return {
        "device_id": device.device_id,
        "status": device.status,
        "matched_as": matched_as,
        "agent_info": agent_info,
        "warnings": warnings,
        "next": "设备已受理；可用 res_list_devices 查询。若为 L3 已入池，申请用 res_apply 指定 device_id 或机型",
    }


def device_flash(session: Session, form_id: str, duration_sec: int = 8) -> dict:
    form = form_store.get(form_id)
    if form is None:
        raise DomainError("FORM_EXPIRED", "表单已失效，请重新调用 res_device_probe")

    model_class = form.probe.get("model")
    has_screen = has_led = False
    if model_class:
        pt = session.get(ProductType, model_class)
        if pt is not None:
            has_screen = pt.has_screen
            has_led = pt.has_led

    import random

    confirm_code = f"{random.randint(0, 9999):04d}"
    if has_screen:
        how = "screen"
    elif has_led:
        how = "led"
    else:
        how = "none"
        confirm_code = ""

    flash_id = new_flash_id()
    form_store.put_flash(flash_id, {"confirm_code": confirm_code, "form_id": form_id, "attempts": 0})
    return {
        "flash_id": flash_id,
        "confirm_code": confirm_code,
        "how": how,
        "next": (
            f"请使用者确认设备上是否显示 {confirm_code} 并闪烁，确认后调用 res_device_confirm 回填该码"
            if how != "none"
            else "该机型无屏无灯，无法点亮核对，直接调用 res_device_register 提交"
        ),
    }


def device_confirm(flash_id: str, code: str) -> dict:
    rec = form_store.get_flash(flash_id)
    if rec is None:
        raise DomainError("NOT_FOUND", "flash_id 不存在")
    if rec["confirm_code"] == code:
        return {"verified": True, "confidence": "high"}
    rec["attempts"] += 1
    if rec["attempts"] >= 3:
        raise DomainError("NEED_ADMIN", "连续 3 次核对失败，需管理员介入")
    return {"verified": False, "confidence": "medium", "attempts_left": 3 - rec["attempts"]}


def device_unregister(session: Session, device_id: str, actor_email: str, is_admin: bool = False) -> dict:
    d = session.get(Device, device_id)
    if d is None:
        raise DomainError("NOT_FOUND", f"device {device_id} not found")

    # 权限：注册人或管理员（10 §1.6）
    if not is_admin and d.registered_by != actor_email:
        raise DomainError("FORBIDDEN", "只有注册人或管理员可以注销该设备")

    from app.models import Lease
    from app.constants import LeaseState

    active = (
        session.query(Lease)
        .filter(Lease.device_id == device_id, Lease.state.in_(LeaseState.ACTIVE))
        .first()
    )
    now = utcnow()
    if active is not None:
        # 有活动租约：标 pending_removal，挂待下架；租约结束后自动注销
        d.status = DeviceStatus.PENDING_REMOVAL
        audit.record(
            session, actor=actor_email, action="unregister_pending_removal",
            device_id=device_id, detail={"lease_id": active.lease_id},
        )
        session.commit()
        raise DomainError(
            "HAS_ACTIVE_LEASE",
            f"该设备有活动租约 {active.lease_id}，已标 pending_removal，租约结束后自动摘除",
            {"lease_id": active.lease_id},
        )

    _retire_device(session, d, actor_email)
    session.commit()
    return {"device_id": device_id, "status": DeviceStatus.RETIRED}


def _retire_device(session: Session, d: Device, actor_email: str) -> None:
    # 通知排队等这台设备的人
    from app.models import QueueEntry
    from app.constants import QueueState

    waiters = (
        session.query(QueueEntry)
        .filter(
            QueueEntry.target_type == "device",
            QueueEntry.target_code == d.device_id,
            QueueEntry.state == QueueState.QUEUED,
        )
        .all()
    )
    if waiters:
        emails = []
        for w in waiters:
            w.state = QueueState.CANCELLED
            if w.principal:
                emails.append(w.principal.email)
        notify_device_unregistered(session, d, list(set(emails)))

    d.status = DeviceStatus.RETIRED
    d.retired_at = utcnow()
    audit.record(session, actor=actor_email, action="unregister", device_id=d.device_id)
