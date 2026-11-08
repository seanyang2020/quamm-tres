"""设备探测抽象（见 04 §8.4 四级探测、10 §2）。

全系统只有这一处探测实现：设备注册（catalog.device_probe）与巡检
（patrol.run_patrol_round）共用 ``get_prober().probe()``，避免两条路径判定不一致。

两种后端由 ``settings.probe_backend`` 选择：

- ``mock``（默认）：GET http://<host>:<port>/probe，解析模拟设备返回的
  model / mac / serial(sn) / asset_tag 等字段。
- ``real``：``adb connect host:port`` + ``adb -s ... shell getprop``，
  按 L0/L1/L2/L3 四级判定。本地无真实板子，real 分支只做实现与 TODO 标注，
  用 mock 后端验证（真实板子联调时再修正 getprop 键名/网卡名）。

地址约定：device 表已有 ``ip`` / ``adb_port``（即探测地址）就直接用；
没有端口时统一按 ``ip:5555`` 组装。调用方只传 host/port，不需要感知后端。
"""
from __future__ import annotations

import json
import logging
import re
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from app.constants import HealthLevel

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


# 兼容旧引用：HttpProber 就是 mock 后端。
HttpProber = MockProber


_PROP_RE = re.compile(r"^\[([^\]]+)\]: \[([^\]]*)\]$", re.MULTILINE)


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    """跑一条外部命令；超时/缺失命令都返回非 0，绝不无限等待。"""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("command timeout (%ss): %s", timeout, " ".join(cmd))
        return 124, ""
    except FileNotFoundError:
        logger.warning("command not found: %s", cmd[0])
        return 127, ""
    except OSError as e:  # noqa: BLE001
        logger.warning("command error %s: %s", " ".join(cmd), e)
        return 1, ""
    return proc.returncode, f"{proc.stdout}\n{proc.stderr}".strip()


def _icmp_reachable(host: str, timeout: int) -> bool | None:
    """ICMP 可达性。返回 None 表示本机 ping 不可用（降级为 TCP 判定）。"""
    rc, out = _run(["ping", "-c", "1", "-W", str(max(1, timeout)), host], timeout)
    if rc == 127:
        return None
    return rc == 0


def _tcp_open(host: str, port: int, timeout: int) -> bool:
    try:
        with socket.create_connection((host, port or DEFAULT_ADB_PORT), timeout=timeout):
            return True
    except OSError:
        return False


class AdbProber:
    """真实 ADB 探测后端（settings.probe_backend=real）。

    四级判定：
    - L0：ICMP/网络连接不通
    - L1：主机可达但 adb 端口不通（connect 失败）
    - L2：端口可达但设备 unauthorized / offline
    - L3：adb shell getprop 可用（并解析设备身份）

    TODO(真实板子联调)：不同厂商的 MAC/asset_tag 取值路径不同，
    这里先读 ro.boot.mac / ro.serialno / /persist/asset_tag；联调时按机型补键名。
    所有外部命令都带 timeout，不会挂起。
    """

    def probe(self, host: str, port: int, timeout: int = 5) -> ProbeResult:
        port = port or DEFAULT_ADB_PORT
        address = f"{host}:{port}"

        icmp = _icmp_reachable(host, timeout)
        if icmp is False and not _tcp_open(host, port, timeout):
            return ProbeResult(level=HealthLevel.L0)
        if icmp is None and not _tcp_open(host, port, timeout):
            return ProbeResult(level=HealthLevel.L0)

        # 端口可达后才尝试 adb connect；失败即 L1（端口不通/连接被拒）。
        rc, out = _run(["adb", "connect", address], timeout)
        lowered = out.lower()
        if rc != 0 or "cannot connect" in lowered or "failed to connect" in lowered:
            logger.info("adb connect failed %s: %s", address, out.replace("\n", " ")[:200])
            return ProbeResult(level=HealthLevel.L1)

        # unauthorized / offline -> L2
        rc, state = _run(["adb", "-s", address, "get-state"], timeout)
        state_l = state.lower()
        if "unauthorized" in state_l or "offline" in state_l:
            return ProbeResult(level=HealthLevel.L2)
        if rc != 0:
            return ProbeResult(level=HealthLevel.L1)

        # shell 可用 -> L3，解析 getprop
        rc, props_out = _run(["adb", "-s", address, "shell", "getprop"], timeout)
        if rc != 0:
            return ProbeResult(level=HealthLevel.L2)

        props = dict(_PROP_RE.findall(props_out))
        model = props.get("ro.product.model")
        sn = props.get("ro.serialno")
        android_version = props.get("ro.build.version.release")
        build_fingerprint = props.get("ro.build.fingerprint")
        mac = props.get("ro.boot.mac") or props.get("ro.mac")

        # asset_tag 是 /persist 文件，取不到不影响 L3。
        tag_rc, tag_out = _run(
            ["adb", "-s", address, "shell", "cat", "/persist/asset_tag"], max(2, timeout // 2)
        )
        asset_tag = tag_out.strip() if tag_rc == 0 and tag_out.strip() else None

        return ProbeResult(
            level=HealthLevel.L3,
            mac=mac,
            sn=sn,
            model=model,
            android_version=android_version,
            build_fingerprint=build_fingerprint,
            asset_tag=asset_tag,
        )


_prober: Prober | None = None


def build_prober(backend: str | None) -> Prober:
    """按配置构造探测后端；未知值回退 mock 并告警。"""
    value = (backend or "mock").strip().lower()
    if value in ("real", "adb"):
        return AdbProber()
    if value not in ("mock", "http"):
        logger.warning("unknown probe_backend=%r, fallback to mock", backend)
    return MockProber()


def set_prober(prober: Prober | None) -> None:
    global _prober
    _prober = prober


def get_prober() -> Prober:
    """取当前探测后端；未显式初始化时按配置懒加载（默认 mock）。"""
    global _prober
    if _prober is None:
        from app.config import settings

        _prober = build_prober(settings.probe_backend)
    return _prober


def probe_device(host: str, port: int = DEFAULT_ADB_PORT, timeout: int = 5) -> ProbeResult:
    return get_prober().probe(host, port, timeout)


def setup_prober_from_settings() -> None:
    from app.config import settings

    set_prober(build_prober(settings.probe_backend))
