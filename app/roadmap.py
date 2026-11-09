"""版本路线图 / 待发布能力（todo）数据源。

结构化维护后续逐步释放的能力，供 MCP 工具 res_roadmap 查询，让使用者提前了解演进方向。
状态：released（已发布）/ in_progress（进行中）/ planned（待发布）/ pending_decision（待拍板）。
"""
from __future__ import annotations

# 每项：id / 标题 / 描述 / 状态 / 类别 / 计划版本（可空）
ROADMAP: list[dict] = [
    # ---- 已发布（近期） ----
    {"id": "R1", "title": "防闲置双闸门", "status": "released", "category": "租约",
     "planned_version": "0.18.0",
     "description": "TTL/心跳逻辑闸门 + RAMFS 物理锁，回收时删锁并断开 ADB。"},
    {"id": "R2", "title": "凭据生命周期管理", "status": "released", "category": "凭据",
     "planned_version": "0.19.0",
     "description": "吊销即级联回收活跃租约，支持恢复、普通删除与管理员强制删除（含关联数据）。"},
    {"id": "R3", "title": "Agent 名展示与硬门禁", "status": "released", "category": "接入",
     "planned_version": "0.19.0",
     "description": "设备 register_agent 与凭据 agent_label；agent_info 为 MCP schema 级必填。"},
    {"id": "R4", "title": "系统介绍内置", "status": "released", "category": "前端",
     "planned_version": "0.19.1",
     "description": "架构/模块介绍作为设置内子标签，自包含离线可用，随版本持续维护。"},
    {"id": "R5", "title": "标签持久化修复", "status": "released", "category": "修复",
     "planned_version": "0.19.3",
     "description": "修复跨会话游离对象导致 agent_label 写库丢失，统一在会话内 merge 身份对象。"},

    # ---- 进行中 / 待发布 ----
    {"id": "T1", "title": "租约到期前主动预警", "status": "planned", "category": "租约",
     "planned_version": "0.20.0",
     "description": "到期/心跳超时前提前向持有人发 warn 或在心跳响应带 expiring_soon，单周期只提醒一次。"},
    {"id": "T2", "title": "设备时钟漂移健康项", "status": "planned", "category": "巡检",
     "planned_version": "0.20.0",
     "description": "巡检新增独立时钟漂移检查，以主机时钟为基准，超阈值提示需 NTP 校时，不改设备时间。"},
    {"id": "T3", "title": "只读接口匿名门禁", "status": "planned", "category": "安全",
     "planned_version": "0.20.0",
     "description": "为当前匿名可访问的只读查询接口加鉴权依赖，未登录返回 401。"},
    {"id": "T4", "title": "长轮询等待 res_wait", "status": "planned", "category": "MCP",
     "planned_version": "0.20.0",
     "description": "提供最长 120s 的长轮询等待，设备空闲即返回，减少客户端轮询。"},

    # ---- 待拍板 ----
    {"id": "D1", "title": "MCP 注册是否收紧为仅管理员", "status": "pending_decision", "category": "安全",
     "planned_version": None,
     "description": "当前任何有效 token 可注册设备（已强制 agent_info），是否限制为管理员待拍板。"},
    {"id": "D2", "title": "串口绑定与编译槽等扩展点", "status": "pending_decision", "category": "MCP",
     "planned_version": None,
     "description": "串口绑定设备 vs 独立排队、编译槽单/多槽、刷机失败是否回滚等待拍板。"},
    {"id": "D3", "title": "历史测试修复", "status": "pending_decision", "category": "质量",
     "planned_version": None,
     "description": "17 个旧测试依赖已清空的 seed 机型，是否修复待拍板。"},
]

_UPCOMING = {"in_progress", "planned"}


def list_roadmap(include_released: bool = False, include_pending: bool = True) -> dict:
    """按状态筛选路线图。默认返回待发布/进行中项，可附带待拍板项。"""
    items = []
    for it in ROADMAP:
        st = it["status"]
        if st in _UPCOMING:
            items.append(it)
        elif st == "pending_decision" and include_pending:
            items.append(it)
        elif st == "released" and include_released:
            items.append(it)
    counts: dict[str, int] = {}
    for it in ROADMAP:
        counts[it["status"]] = counts.get(it["status"], 0) + 1
    return {"total": len(items), "status_counts": counts, "items": items}
