"""真实 Quamm XOS 设备探测（settings.probe_backend=real / device.source=real）。

Quamm XOS 是精简嵌入式 Linux，**不是标准 Android，没有 getprop 命令**
（``adb shell getprop`` 会报 not found）。因此身份指纹全部靠 ``cat`` 读文件采集，
绝不调用 getprop，也不 push / remount / 刷机。

流程：``adb connect ip:port``（超时 3s）→ ``adb -s ip:port shell <cmd>`` 逐个读文件
→ ``adb disconnect``。连不上 / 读不到 → L0；读到 product_name 且有固定 mac → L3。

固定 mac 口径：eth0 存在且不以 ``fe:`` 开头则取 eth0；否则取不以 ``fe:`` 开头的
wlan0；否则为 None。``fe:``（本地管理 / 随机化）地址不能当设备主键。

所有 adb 命令同时用 GNU ``timeout`` 与 subprocess 超时双重包裹，绝不无限挂起。
"""
from __future__ import annotations

import logging
import re
import subprocess

from app.constants import HealthLevel

logger = logging.getLogger(__name__)

ADB_TIMEOUT = 3  # 单条 adb 命令最长 3s（connect / shell / disconnect 统一口径）

_MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
_TOKEN_RE = re.compile(r"[a-z0-9_]{1,64}")
# v0.14：设备固件版本。实测 XOS 落在 /etc/fw_version（如 V1.00.01.236 / 1.0.0），
# 部分工程才有 /etc/version。只认「数字点分」形态，挡掉 shell 横幅与报错。
_VERSION_RE = re.compile(r"[Vv]?\d+(?:\.\d+){1,3}")


def _run(cmd: list[str]) -> tuple[int, str]:
    """跑一条 adb 命令；GNU timeout + subprocess 超时双保险，不挂起。

    返回 (returncode, stdout.strip())。stderr 单独丢弃：cat 失败的错误文本
    不带干净 token / MAC，不会被下面的解析误当成有效值。
    """
    full = ["timeout", str(ADB_TIMEOUT), "adb", *cmd]
    try:
        proc = subprocess.run(
            full,
            capture_output=True,
            text=True,
            timeout=ADB_TIMEOUT + 1,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, ""
    except (FileNotFoundError, OSError):
        return 127, ""
    return proc.returncode, (proc.stdout or "").strip()


def _first_token(out: str) -> str | None:
    """从 cat /etc/product_name 的输出里取干净的工程配置名。

    XOS 的登录 shell 可能先打印 /etc/profile 横幅，cat 失败也会打印错误行；
    这些行含空格 / 大写 / 冒号，不会被 ``[a-z0-9_]+`` 命中。
    """
    for line in out.replace("\r", "\n").split("\n"):
        s = line.strip()
        if _TOKEN_RE.fullmatch(s):
            return s
    return None


def _first_mac(out: str) -> str | None:
    for line in out.replace("\r", "\n").split("\n"):
        s = line.strip().lower()
        if _MAC_RE.fullmatch(s):
            return s
    return None


def _first_version(out: str) -> str | None:
    """从 cat /etc/fw_version 输出里取版本串（V1.00.01.236 / 1.0.0）。"""
    for line in out.replace("\r", "\n").split("\n"):
        s = line.strip()
        m = _VERSION_RE.fullmatch(s)
        if m:
            return s
    return None


def read_firmware_version(host: str, port: int = 5555) -> str | None:
    """读设备当前固件版本；连不上 / 无文件返回 None（刷机后回读用）。

    /etc/fw_version 是 XOS 实际落点；老工程可能只有 /etc/version。
    """
    address = f"{host}:{port or 5555}"
    rc, out = _run(["connect", address])
    if "connected" not in out.lower():
        _run(["disconnect", address])
        return None
    try:
        _, fw_out = _run(["-s", address, "shell", "cat /etc/fw_version"])
        version = _first_version(fw_out)
        if version is None:
            _, ver_out = _run(["-s", address, "shell", "cat /etc/version"])
            version = _first_version(ver_out)
        return version
    finally:
        _run(["disconnect", address])


def _is_fixed(mac: str | None) -> bool:
    """fe: 开头是本地管理 / 随机化地址，不能当主键。"""
    return bool(mac) and not mac.lower().startswith("fe:")


class RealXosProber:
    """真实 XOS 设备探测后端。

    probe(host, port, timeout) 与 Prober 协议一致；返回
    ``app.domain.probe.ProbeResult``（在此方法内延迟导入，避免与 probe.py 循环引用）。
    额外字段放在 ``result.extra``。
    """

    def probe(self, host: str, port: int = 5555, timeout: int = ADB_TIMEOUT) -> object:
        from app.domain.probe import ProbeResult  # 延迟导入，打破循环引用

        address = f"{host}:{port or 5555}"
        product_name: str | None = None
        eth_mac: str | None = None
        wlan_mac: str | None = None
        storage_type = "unknown"
        has_wifi = False
        board_dt: str | None = None
        firmware_version: str | None = None

        # 1) connect（3s 超时）。输出含 connected 才算连上；offline / 不可达直接 L0。
        rc, out = _run(["connect", address])
        connected = "connected" in out.lower()
        if not connected:
            logger.info("real probe connect failed %s rc=%s: %s", address, rc, out[:120])
            _run(["disconnect", address])
            return self._result(ProbeResult, HealthLevel.L0, None, None, None, None, "unknown", False, None, None)

        try:
            # 2) 逐个读文件（每条命令都带 timeout；文件不存在时 stdout 为空 -> 跳过）
            _, pn_out = _run(["-s", address, "shell", "cat /etc/product_name"])
            product_name = _first_token(pn_out)

            _, eth_out = _run(["-s", address, "shell", "cat /sys/class/net/eth0/address"])
            eth_mac = _first_mac(eth_out)  # 无 eth0 -> None（跳过）

            _, wlan_out = _run(["-s", address, "shell", "cat /sys/class/net/wlan0/address"])
            wlan_mac = _first_mac(wlan_out)

            _, cmd_out = _run(["-s", address, "shell", "cat /proc/cmdline"])
            if "spi_nor" in cmd_out:
                storage_type = "nor"
            elif "spi_nfc" in cmd_out:
                storage_type = "nand"

            _, wifi_out = _run(
                ["-s", address, "shell", "test -d /sys/class/net/wlan0 && echo QUAMM_WIFI_OK"]
            )
            has_wifi = "QUAMM_WIFI_OK" in wifi_out

            _, dt_out = _run(
                ["-s", address, "shell", "cat /proc/device-tree/compatible"]
            )
            # device-tree compatible 是 \0 分隔的多串，替换为空格后折成一行。
            joined_dt = " ".join(dt_out.replace("\x00", " ").split())
            # 只认含 "xxx,yyy" 逗号对的 compatible 串；挡掉登录 shell 横幅垃圾。
            board_dt = joined_dt if re.search(r"[a-z0-9]+,[a-z0-9]+", joined_dt) else None

            _, fw_out = _run(["-s", address, "shell", "cat /etc/fw_version"])
            firmware_version = _first_version(fw_out)
            if firmware_version is None:
                _, ver_out = _run(["-s", address, "shell", "cat /etc/version"])
                firmware_version = _first_version(ver_out)
        finally:
            _run(["disconnect", address])

        # 固定 mac：eth0 优先（非 fe:），否则 wlan0（非 fe:），否则 None
        if _is_fixed(eth_mac):
            fixed_mac = eth_mac
        elif _is_fixed(wlan_mac):
            fixed_mac = wlan_mac
        else:
            fixed_mac = None

        level = HealthLevel.L3 if (product_name and fixed_mac) else HealthLevel.L0
        return self._result(
            ProbeResult, level, fixed_mac, product_name, eth_mac, wlan_mac,
            storage_type, has_wifi, board_dt, firmware_version,
        )

    @staticmethod
    def _result(
        ProbeResult, level, fixed_mac, product_name, eth_mac, wlan_mac,
        storage_type, has_wifi, board_dt, firmware_version,
    ):
        return ProbeResult(
            level=level,
            mac=fixed_mac,
            sn=None,                       # XOS /proc/cpuinfo Serial 全 0，无 SN
            model=product_name,            # 工程配置名即机型/类型
            android_version=None,
            build_fingerprint=firmware_version,
            asset_tag=None,
            extra={
                "product_name": product_name,
                "project_name": product_name,
                "eth_mac": eth_mac,
                "wlan_mac": wlan_mac,
                "fixed_mac": fixed_mac,
                "storage_type": storage_type,
                "has_wifi": has_wifi,
                "board_dt": board_dt,
                "firmware_version": firmware_version,
            },
        )
