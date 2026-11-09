"""应用配置：全部参数集中在此，便于整体迁移到别的局域网 Ubuntu。

生产环境通过环境变量 / deploy/.env 覆盖；本地开发直接使用默认值。
"""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RES_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 数据库
    database_url: str = "sqlite:///./quamm_res.db"

    # 租约
    lease_ttl_minutes: int = 90          # 默认 TTL
    lease_grace_minutes: int = 10        # 宽限期
    lease_max_minutes: int = 1440        # 单次续约累计上限（24h）
    offer_window_seconds: int = 60       # 队首 offer 确认窗口
    holder_concurrency_limit: int = 2    # 单人并发占用上限
    lease_scan_seconds: int = 10         # 租约到期扫描周期

    # 心跳（v0.18）：Agent 必须周期性确认仍在使用，超时未心跳由 worker 强制回收
    heartbeat_required: bool = True
    heartbeat_interval_minutes: int = 75     # 期望心跳间隔（兼容不支持小时内定时器的 Agent）
    heartbeat_miss_grace_minutes: int = 10   # 超过间隔后的宽限，仍未心跳即回收
    # 设备侧 RAMFS 文件锁路径（tmpfs，重启清空）；锁由 Agent 用主机时间生成并维护
    device_lock_path: str = "/tmp/quamm_tres.lock"

    # 凭据
    credential_valid_days: int = 90
    token_prefix: str = "rr_"

    # 巡检
    patrol_interval_minutes: int = 30
    # v0.28：pending（待检）设备专项探测间隔。用户口径：待检设备每 10 分钟探一次，
    # 尽快转正，别等到 30 分钟全量巡检；可在设置页在线调整。
    pending_patrol_interval_minutes: int = 10
    patrol_fail_threshold: int = 2       # 连续 N 轮失败判 offline
    probe_timeout_seconds: int = 5
    # 探测后端：mock=模拟设备 HTTP /probe（默认，本地演示）；
    # real=真实 Quamm XOS 设备，adb connect + shell 读文件（无 getprop）。
    # 决定"按 IP 探测"的全局默认；巡检则按每台 device.source 分流（见 probe.get_prober_for）。
    probe_backend: str = "mock"          # mock | real

    # 可用池口径：suspect（单轮失败、降级未掉线）默认不可被申请，
    # 避免 Agent 拿到一台连不上的设备；需要"降级仍可用"时置 True。
    allow_apply_when_suspect: bool = False

    # 设备网络（worker 网段发现用，一期预留）
    device_network: str = ""

    # 截图保留策略（v0.24，可在「设置-截图存储」在线修改，运行时 KV 覆盖此处默认）
    screenshot_max_per_device: int = 10       # 每台设备最多保留的最新截图条数
    screenshot_retention_days: int = 14       # 全局保留天数（超过即删）
    screenshot_total_max: int = 500           # 全局最多保留的截图总条数
    screenshot_cleanup_on_release: bool = True  # 换用户/释放时清空该设备截图
    # pending 设备端口敲门自动恢复（等价 adbscan -k -e <eventcode>）
    pending_auto_recover_enabled: bool = True   # 巡检对 ping 通但 adb 未起的 pending 设备自动敲门
    pending_knock_eventcode: str = "2223"       # 端口敲门事件码（3–6 位数字）
    pending_knock_timeout: int = 30             # 敲门后等待 adbd 起来的秒数

    # 固件下载 / 刷机（v0.14，复用现成工具，不重新实现）
    xos_base_url: str = "http://172.16.1.201/Daily/XOS"  # DB 目录根（末尾无 /）
    dbxostest_path: str = "/home/scm/bin/dbxostest.py"   # 下载 + 刷写编排器
    python_bin: str = "python3"
    flash_work_dir: str = "./var/flash"                  # dbxostest 的 cwd（downloads/tmp 落此）
    flash_log_dir: str = "./var/flash/logs"              # 每个 job 一份日志
    flash_http_timeout: int = 8                          # 版本目录列表 HTTP 超时
    flash_timeout_seconds: int = 1800                    # 单次刷机子进程上限（5-15 分钟）

    # 通知（一期只落库，不真正发信）
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""

    # 服务
    app_domain: str = "res.corp.com"
    web_port: int = 9100
    mcp_port: int = 8765

    # 管理员凭据（用于 res_admin 与 Web 管理；一期固定环境变量，未配则默认）
    admin_token: str = ""

    # 是否自动建表 / 是否加载 seed
    auto_create_tables: bool = True

    # Web 会话 cookie（HttpOnly，8 小时）签名密钥；生产可用环境变量覆盖
    session_secret: str = "quamm-tres-local-session-secret"
    web_session_hours: int = 8
    # 发凭据邮件里 MCP 配置对外暴露的地址
    mcp_public_url: str = "http://172.16.10.25:9100/mcp/"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
