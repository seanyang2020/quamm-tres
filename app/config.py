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
    lease_ttl_minutes: int = 30          # 默认 TTL
    lease_grace_minutes: int = 10        # 宽限期
    lease_max_minutes: int = 240         # 单次续约累计上限
    offer_window_seconds: int = 60       # 队首 offer 确认窗口
    holder_concurrency_limit: int = 2    # 单人并发占用上限
    lease_scan_seconds: int = 10         # 租约到期扫描周期

    # 凭据
    credential_valid_days: int = 90
    token_prefix: str = "rr_"

    # 巡检
    patrol_interval_minutes: int = 30
    patrol_fail_threshold: int = 2       # 连续 N 轮失败判 offline
    probe_timeout_seconds: int = 5
    prober_backend: str = "adb"          # adb=真实(一期 TODO) / http=模拟设备

    # 设备网络（worker 网段发现用，一期预留）
    device_network: str = ""

    # 通知（一期只落库，不真正发信）
    smtp_host: str = ""
    smtp_port: int = 25
    smtp_user: str = ""
    smtp_password: str = ""
    mail_from: str = ""

    # 服务
    app_domain: str = "res.corp.com"
    web_port: int = 8080
    mcp_port: int = 8765

    # 管理员凭据（用于 res_admin 与 Web 管理；一期固定环境变量，未配则默认）
    admin_token: str = ""

    # 是否自动建表 / 是否加载 seed
    auto_create_tables: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
