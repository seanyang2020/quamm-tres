"""进程运行时状态：记录本次启动的墙钟时间，供健康检查与前端展示运行时长。

注意：uvicorn 与 worker 是独立进程，各自有一份 STARTED_AT；
/health 由 uvicorn 提供，因此展示的是 Web 服务进程的启动时间/运行时长。
"""
from __future__ import annotations

from app.utils import utcnow

# 模块导入即进程启动时刻（naive UTC）
STARTED_AT = utcnow()


def uptime_seconds() -> float:
    return (utcnow() - STARTED_AT).total_seconds()


def format_uptime() -> str:
    """把运行时长格式化为 天/时/分/秒 的可读字符串。"""
    total = int(uptime_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}天")
    if hours or days:
        parts.append(f"{hours}小时")
    parts.append(f"{minutes}分{seconds}秒")
    return "".join(parts)
