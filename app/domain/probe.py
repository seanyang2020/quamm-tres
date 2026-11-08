"""设备探测抽象（见 04 §8.4 四级探测、10 §2）。

全系统只有这一处探测入口：设备注册（catalog.device_probe）与巡检
（patrol.run_patrol_round）共用探测，避免两条路径判定不一致。

按 device.source 分流（巡检 / 明确指定来源的探测）：
- ``mock``（默认）：GET http://<host>:<port>/probe，解析模拟设备返回的
  model / mac / serial(sn) / asset_tag 等字段。
- ``real``：真实 Quamm XOS 设备的 ADB 文件探测（app.domain.prober_real.RealXosProber）。
  XOS 不是标准 Android、无 getprop，靠 cat /etc/product_name 等读文件采集，绝不调用 getprop。

settings.probe_backend 决定"按 IP 探测"（device_probe，尚无 device 记录时）的
全局默认后端：mock / real。巡检则强制按每台设备的 source 分流，保证 22 台
mock 设备永远走 HTTP /probe，不会被全局 real 后端误伤。

地址约定：device 表已有 ip / adb_port（即探测地址）就直接用；
没有端口时统一按 ip:5555 组装。调用方只传 host/port，不需要感知后端。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from app.constants import HealthLevel
from app.domain.prober_real import RealXosProber

logger = logging.getLogger(__name__)

DEFAULT_ADB_PORT = 5555


@dataclass
class ProbeResult:
    level: str  # L0 / L1 / L2 / L3
    mac: str | None = None
    sn: str | None = None
    model: str | None = None
    android_version: str | None = None
    build_fingerprint: str | None = None
    asset_tag: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def reachable_l3(self) -> bool:
        return self.level == HealthLevel.L3

    @property
    def serial(self) -> str | None:
        """serial 与 sn 同义，便于按协议字段名读取。"""
        return self.sn


class Prober(Protocol):
    def probe(self, host: str, port: int, timeout: int = 5) -> ProbeResult: ...


class MockProber:
    """模拟设备探测后端（测试 / 演示默认）。

    模拟设备协议：GET http://host:port/probe ->
    {"level": "L3", "mac": ..., "sn"/"serial": ..., "model": ..., "asset_tag": ...}
    L0 = 连接失败（设备不在线）；L1/L2 = 设备在线但返回对应 level。
    """

    def probe(self, host: str, port: int, timeout: int = 5) -> ProbeResult:
        url = f"http://{host}:{port or DEFAULT_ADB_PORT}/probe"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            logger.debug("mock probe unreachable %s:%s: %s", host, port, e)
            return ProbeResult(level=HealthLevel.L0)
        except Exception as e:  # noqa: BLE001
            logger.warning("mock probe error %s:%s: %s", host, port, e)
            return ProbeResult(level=HealthLevel.L0)

        level = data.get("level", HealthLevel.L3)
        if level != HealthLevel.L3:
            return ProbeResult(level=level)

        return ProbeResult(
            level=HealthLevel.L3,
            mac=data.get("mac"),
            sn=data.get("sn") or data.get("serial"),
            model=data.get("model"),
            android_version=data.get("android_version"),
            build_fingerprint=data.get("build_fingerprint"),
            asset_tag=data.get("asset_tag"),
        )


# 兼容旧引用。
HttpProber = MockProber                 # mock 后端旧名
AdbProber = RealXosProber              # 真实后端旧名（现为 XOS 文件探测，无 getprop）


_prober: Prober | None = None


def build_prober(backend: str | None) -> Prober:
    """按配置构造探测后端；未知值回退 mock 并告警。"""
    value = (backend or "mock").strip().lower()
    if value in ("real", "adb"):
        return RealXosProber()
    if value not in ("mock", "http"):
        logger.warning("unknown probe_backend=%r, fallback to mock", backend)
    return MockProber()


def set_prober(prober: Prober | None) -> None:
    global _prober
    _prober = prober


def get_prober() -> Prober:
    """取当前全局探测后端；未显式初始化时按配置懒加载（默认 mock）。"""
    global _prober
    if _prober is None:
        from app.config import settings

        _prober = build_prober(settings.probe_backend)
    return _prober


def get_prober_for(source: str | None) -> Prober:
    """按 device.source 选择探测后端（巡检按台分流，不受全局 backend 影响）。

    - ``real``：真实 XOS ADB 文件探测
    - ``mock``：强制走模拟设备 HTTP /probe（即使全局 backend=real 也不受影响）
    - 未标注（None）：沿用全局 prober（兼容旧库与测试里 set_prober 注入）
    """
    s = (source or "").strip().lower()
    if s == "real":
        return RealXosProber()
    if s == "mock":
        return MockProber()
    return get_prober()


def probe_device(
    host: str, port: int = DEFAULT_ADB_PORT, timeout: int = 5, source: str | None = None
) -> ProbeResult:
    """探测一台设备；给出 source 时按台分流，否则用全局后端。"""
    return get_prober_for(source).probe(host, port, timeout)


def setup_prober_from_settings() -> None:
    from app.config import settings

    set_prober(build_prober(settings.probe_backend))
