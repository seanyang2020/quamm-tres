"""设备探测抽象（见 04 §8.4 四级探测、10 §2）。

真实 ADB 探测在一期不实现，只留接口位置（AdbProber，TODO）。
模拟阶段使用 HttpProber 连接 tests/mock_device.py 的假 ADB 端点。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol

from app.constants import HealthLevel

logger = logging.getLogger(__name__)


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


class Prober(Protocol):
    def probe(self, ip: str, port: int, timeout: int = 5) -> ProbeResult: ...


class AdbProber:
    """真实 ADB 探测。

    TODO(一期不实现)：接入 adb connect / adb devices / adb shell getprop。
    接口位置即本类，后续替换 probe() 实现即可，领域层不感知。
    """

    def probe(self, ip: str, port: int, timeout: int = 5) -> ProbeResult:
        # 一期不做真实 ADB 连接，返回 L0 表示不可达。
        logger.warning("AdbProber.probe is a stub; real ADB probe not implemented")
        return ProbeResult(level=HealthLevel.L0)


class HttpProber:
    """连接模拟设备的探测后端（测试 / 演示用）。

    模拟设备协议：GET http://ip:port/probe -> {"level": "L3", "mac": ..., "sn": ..., ...}
    L0 = 连接失败（设备不在线）；L1/L2 = 设备在线但返回对应 level。
    """

    def probe(self, ip: str, port: int, timeout: int = 5) -> ProbeResult:
        import urllib.error
        import urllib.request

        url = f"http://{ip}:{port}/probe"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                import json

                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            logger.debug("probe unreachable %s:%s: %s", ip, port, e)
            return ProbeResult(level=HealthLevel.L0)
        except Exception as e:  # noqa: BLE001
            logger.warning("probe error %s:%s: %s", ip, port, e)
            return ProbeResult(level=HealthLevel.L0)

        level = data.get("level", HealthLevel.L3)
        if level != HealthLevel.L3:
            return ProbeResult(level=level)

        return ProbeResult(
            level=HealthLevel.L3,
            mac=data.get("mac"),
            sn=data.get("sn"),
            model=data.get("model"),
            android_version=data.get("android_version"),
            build_fingerprint=data.get("build_fingerprint"),
            asset_tag=data.get("asset_tag"),
        )


_prober: Prober = AdbProber()


def set_prober(prober: Prober) -> None:
    global _prober
    _prober = prober


def get_prober() -> Prober:
    return _prober


def probe_device(ip: str, port: int, timeout: int = 5) -> ProbeResult:
    return get_prober().probe(ip, port, timeout)


def setup_prober_from_settings() -> None:
    from app.config import settings

    if settings.prober_backend == "http":
        set_prober(HttpProber())
    else:
        set_prober(AdbProber())
