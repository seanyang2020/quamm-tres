"""真实 Quamm XOS 设备探测（settings.probe_backend=real / device.source=real）。

Quamm XOS 是精简嵌入式 Linux，**不是标准 Android，没有 getprop 命令**
（``adb shell getprop`` 会报 not found）。因此身份指纹全部靠 ``cat`` 读文件采集，
绝不调用 getprop，也不 push / remount / 刷机。

流程：``adb connect ip:port``（超时 3s）→ ``adb -s ip:port shell <cmd>`` 逐个读文件
→ ``adb disconnect``。

v0.28.2 入池口径：**adb 连得上且 shell 能执行即 L3**（用 ``echo __TRES_SHELL_OK__``
探针判定）。product_name / MAC / 固件版本降级为「有则采集」的元数据，不再作为入池门槛——
esl2 一类设备没有 eth0、wlan MAC 又是 ``fe:`` 本地管理地址，按旧口径会永久 L0 无法入池。
连不上、或 shell 起不来（``adbd: exec '/bin/sh' failed``、``device offline``）仍判 L0。

固定 mac 口径（仅作登记指纹，不再是入池条件）：eth0 有地址则取 eth0，否则取 wlan0，
都没有为 None。v0.28.2 起 ``fe:``（local-admin 位）地址**也采信**——实测 esl2 的
``fe:xx`` 地址跨会话稳定，属烧录固定值；仅排除全零地址。``extra`` 里同时记录
``mac_source`` 与 ``mac_is_local_admin``，便于后续排错。

所有 adb 命令同时用 GNU ``timeout`` 与 subprocess 超时双重包裹，绝不无限挂起。

v0.23：额外读 ``/proc/cpuinfo``，把 features / cpu_part / cpu_implementer /
has_fpu（Features 含 vfp/neon）放进 ``ProbeResult.extra``，供设备截图选浮点 ABI。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import socket
import subprocess

from app.constants import HealthLevel

logger = logging.getLogger(__name__)

ADB_TIMEOUT = 5  # 单条 adb 命令默认超时（对齐 adbscan；并发扫描时由调用方传入更大值）
# v0.30.1：adb connect 单独放宽。冷启动 adb server（或对上位旧版 client 重起 server）
# 实测会吃掉 5s，导致巡检把在线设备误判 L0/offline。
ADB_CONNECT_TIMEOUT = 15  # 端口预检已滤掉不可达设备，剩下的要给足 adb 握手时间：
# adbd 冷启/刚重启时首次 connect 常需 10s+，5s 会一直 rc=124 导致设备永远无法自动找回

# v0.30.1：TCP 端口预检窗口。不可达设备（离线占多数）用 socket 亚秒级排除，
# 不再为每台付满 adb connect 超时；adb 只用于端口已通的设备。
TCP_PREFLIGHT_TIMEOUT = 2.0


def tcp_port_open(host: str, port: int, timeout: float = TCP_PREFLIGHT_TIMEOUT) -> bool:
    """socket 直连 5555，判断 adbd 端口是否可达（不启动 adb，不做协议握手）。"""
    if not host:
        return False
    try:
        with socket.create_connection((host, port or 5555), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False

# v0.30.1：显式解析 adb 二进制。系统 /usr/bin/adb（Ubuntu 包 1.0.39）与 platform-tools
# 1.0.41 的 server 版本不一致，会互杀 server 并 drag 住 connect，必须避让。
_ADB_CANDIDATES = (
    "~/bin/adb",
    "~/opt/platform-tools/adb",
    "/usr/local/bin/adb",
)


def _resolve_adb() -> str:
    """按固定优先级找 adb；环境变量 TRES_ADB_PATH 可覆盖。"""
    env = (os.environ.get("TRES_ADB_PATH") or "").strip()
    if env:
        return env
    for cand in _ADB_CANDIDATES:
        path = os.path.expanduser(cand)
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    found = shutil.which("adb") or "adb"
    logger.warning("adb 未命中首选路径，回退 %s（可能是系统旧版 1.0.39，会影响 server 复用）", found)
    return found


ADB_BIN = _resolve_adb()

# v0.28.2：shell 活性探针。大写 + 双下划线，不会被 _TOKEN_RE / _MAC_RE 误命中。
_SHELL_PROBE = "echo __TRES_SHELL_OK__"

_MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
_TOKEN_RE = re.compile(r"[a-z0-9_]{1,64}")
# v0.14：设备固件版本。实测 XOS 落在 /etc/fw_version（如 V1.00.01.236 / 1.0.0），
# 部分工程才有 /etc/version。只认「数字点分」形态，挡掉 shell 横幅与报错。
_VERSION_RE = re.compile(r"[Vv]?\d+(?:\.\d+){1,3}")
# v0.23：/proc/cpuinfo 的 Features / CPU part / CPU implementer 行（XOS 无 getprop）。
_CPU_FEATURES_RE = re.compile(r"^\s*Features\s*:\s*(.+)$", re.I | re.M)
_CPU_PART_RE = re.compile(r"^\s*CPU part\s*:\s*(\S+)", re.I | re.M)
_CPU_IMPL_RE = re.compile(r"^\s*CPU implementer\s*:\s*(\S+)", re.I | re.M)


def _run(cmd: list[str], timeout: int = ADB_TIMEOUT) -> tuple[int, str]:
    """跑一条 adb 命令；GNU timeout + subprocess 超时双保险，不挂起。

    返回 (returncode, stdout.strip())。stderr 单独丢弃：cat 失败的错误文本
    不带干净 token / MAC，不会被下面的解析误当成有效值。
    """
    full = ["timeout", str(timeout), ADB_BIN, *cmd]
    try:
        proc = subprocess.run(
            full,
            capture_output=True,
            text=True,
            timeout=timeout + 1,
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


def _parse_cpuinfo(out: str) -> tuple[str | None, str | None, str | None, bool]:
    """解析 cat /proc/cpuinfo → (features, cpu_part, cpu_implementer, has_fpu)。

    has_fpu：Features 含 vfp（含 vfpv3/vfpv4）或 neon 即为硬件浮点；
    实测 qm10xv_full(Cortex-A7, part 0xc07) Features 为
    "half thumb fastmult vfp edsp neon vfpv3 ..."。
    """
    text = out.replace("\r", "\n")
    m = _CPU_FEATURES_RE.search(text)
    features = " ".join(m.group(1).split()) if m else None
    has_fpu = False
    if features:
        has_fpu = any(t.startswith("vfp") or t == "neon" for t in features.lower().split())
    m = _CPU_PART_RE.search(text)
    cpu_part = m.group(1) if m else None
    m = _CPU_IMPL_RE.search(text)
    cpu_implementer = m.group(1) if m else None
    return features, cpu_part, cpu_implementer, has_fpu


def read_firmware_version(host: str, port: int = 5555) -> str | None:
    """读设备当前固件版本；连不上 / 无文件返回 None（刷机后回读用）。

    /etc/fw_version 是 XOS 实际落点；老工程可能只有 /etc/version。
    """
    address = f"{host}:{port or 5555}"
    rc, out = _run(["connect", address], ADB_CONNECT_TIMEOUT)
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


# v0.31：高精度工程配置指纹。
# 新构建固件在 /etc 下带 build-info（key=value：project/platform/built_at/git/defconfig）
# 与 config.gz（内核 .config 的 gzip：权威品类 CONFIG_PRODUCT_X=y、QM_VERSION、能力位）。
_ENG_GREP = (
    "zcat /etc/config.gz 2>/dev/null | grep -E '"
    "CONFIG_PRODUCT_[A-Z0-9_]+=y|CONFIG_XOS_PROJECT_TYPE=|CONFIG_XOS_APP_MAIN_ENTRY=|"
    "CONFIG_QM_VERSION=|CONFIG_QM_HW_VERSION=|CONFIG_PLATFORM_ID=|"
    "CONFIG_LCD_PANEL=|CONFIG_TOUCH_PANEL=|CONFIG_WIFI_SUPPORT=|CONFIG_ETHERNET_SUPPORT=|"
    "CONFIG_XOS_WIFI_QUADRV='"
)

_ENG_KEY_MAP = {
    "CONFIG_XOS_PROJECT_TYPE": "project_type",
    "CONFIG_XOS_APP_MAIN_ENTRY": "main_entry",
    "CONFIG_QM_VERSION": "qm_version",
    "CONFIG_QM_HW_VERSION": "hw_version",
    "CONFIG_PLATFORM_ID": "platform_id",
    "CONFIG_XOS_WIFI_QUADRV": "wifi_chip",
}

_ENG_FLAG_MAP = {
    "CONFIG_LCD_PANEL": "has_lcd",
    "CONFIG_TOUCH_PANEL": "has_touch",
    "CONFIG_WIFI_SUPPORT": "cfg_has_wifi",
    "CONFIG_ETHERNET_SUPPORT": "has_ethernet",
}


def _parse_kv(out: str) -> dict[str, str]:
    """解析 key=value 行（/etc/build-info）。注释与空行跳过。"""
    result: dict[str, str] = {}
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        if key:
            result[key] = val.strip()
    return result


def _parse_eng_config(out: str) -> dict:
    """解析 /etc/config.gz 的关键行，产出权威品类与能力位。

    has_lcd/has_touch/has_ethernet/cfg_has_wifi 来自编译期配置，是设备能力画像；
    product 是 CONFIG_PRODUCT_X=y 命中的品类（比 /etc/product_name 更权威）。
    """
    cfg: dict = {}
    product: str | None = None
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"')
        lowered = val.lower()
        if key.startswith("CONFIG_PRODUCT_") and lowered == "y":
            # CONFIG_PRODUCT_BOARD 是字符串键，不会走到这里；其余命中即品类
            if product is None and key != "CONFIG_PRODUCT_BOARD":
                product = key[len("CONFIG_PRODUCT_"):]
            continue
        if key in _ENG_KEY_MAP:
            cfg[_ENG_KEY_MAP[key]] = val
        elif key in _ENG_FLAG_MAP:
            cfg[_ENG_FLAG_MAP[key]] = lowered == "y"
    if product:
        cfg["product"] = product
    return cfg


def _is_fixed(mac: str | None) -> bool:
    """是否可作为设备指纹。

    v0.28.2：**不再排除 fe: 前缀**。实测 esl2 类设备只有 wlan0，地址形如
    ``fe:47:b5:cd:b5:0d``，跨多次 connect/disconnect 读取完全一致——它是烧录的
    固定地址，只是带 local-admin 位；按前缀一刀切会把正常设备判成无指纹。
    仅排除空值与全零地址。
    """
    return bool(mac) and mac.strip().lower() != "00:00:00:00:00:00"


class RealXosProber:
    """真实 XOS 设备探测后端。

    probe(host, port, timeout) 与 Prober 协议一致；返回
    ``app.domain.probe.ProbeResult``（在此方法内延迟导入，避免与 probe.py 循环引用）。
    额外字段放在 ``result.extra``。
    """

    def probe(self, host: str, port: int = 5555, timeout: int = ADB_TIMEOUT) -> object:
        from app.domain.probe import ProbeResult  # 延迟导入，打破循环引用

        address = f"{host}:{port or 5555}"
        # v0.30.1：端口预检。端口不通直接 L0，省掉 adb connect 的 10s 超时。
        if not tcp_port_open(host, port or 5555):
            logger.info("real probe tcp closed %s", address)
            return self._result(
                ProbeResult, HealthLevel.L0, None, None, None, None,
                "unknown", False, None, None,
            )
        product_name: str | None = None
        eth_mac: str | None = None
        wlan_mac: str | None = None
        storage_type = "unknown"
        has_wifi = False
        board_dt: str | None = None
        firmware_version: str | None = None
        features: str | None = None
        cpu_part: str | None = None
        cpu_implementer: str | None = None
        has_fpu = False
        mac_source: str | None = None
        build_info: dict = {}
        eng_config: dict = {}

        # 1) connect（超时）。输出含 connected 才算连上；offline / 不可达直接 L0。
        rc, out = _run(["connect", address], max(timeout, ADB_CONNECT_TIMEOUT))
        connected = "connected" in out.lower()
        if not connected:
            logger.info("real probe connect failed %s rc=%s: %s", address, rc, out[:120])
            _run(["disconnect", address], timeout)
            return self._result(ProbeResult, HealthLevel.L0, None, None, None, None, "unknown", False, None, None)

        shell_ok = False
        try:
            # 2) shell 活性探针：adb 连得上 ≠ shell 可执行（adbd exec 失败、offline 都要判 L0）
            _, alive_out = _run(["-s", address, "shell", _SHELL_PROBE], timeout)
            shell_ok = "__TRES_SHELL_OK__" in alive_out
            if not shell_ok:
                # 并发扫描下首次可能被饿死，放宽超时补一次
                _, alive_out = _run(
                    ["-s", address, "shell", _SHELL_PROBE], max(timeout + 2, 8)
                )
                shell_ok = "__TRES_SHELL_OK__" in alive_out
            if not shell_ok:
                logger.info("real probe shell dead %s: %s", address, alive_out[:120])

            # 3) 逐个读文件（每条命令都带 timeout；文件不存在时 stdout 为空 -> 跳过）
            _, pn_out = _run(["-s", address, "shell", "cat /etc/product_name"], timeout)
            product_name = _first_token(pn_out)
            # 并发扫描下首次读取可能被饿死，空结果时补一次（放宽超时）。
            if product_name is None:
                _, pn_out = _run(
                    ["-s", address, "shell", "cat /etc/product_name"], max(timeout + 2, 8)
                )
                product_name = _first_token(pn_out)

            _, eth_out = _run(["-s", address, "shell", "cat /sys/class/net/eth0/address"], timeout)
            eth_mac = _first_mac(eth_out)  # 无 eth0 -> None（跳过）

            _, wlan_out = _run(["-s", address, "shell", "cat /sys/class/net/wlan0/address"], timeout)
            wlan_mac = _first_mac(wlan_out)

            _, cmd_out = _run(["-s", address, "shell", "cat /proc/cmdline"], timeout)
            if "spi_nor" in cmd_out:
                storage_type = "nor"
            elif "spi_nfc" in cmd_out:
                storage_type = "nand"

            _, wifi_out = _run(
                ["-s", address, "shell", "test -d /sys/class/net/wlan0 && echo QUAMM_WIFI_OK"],
                timeout,
            )
            has_wifi = "QUAMM_WIFI_OK" in wifi_out

            _, dt_out = _run(
                ["-s", address, "shell", "cat /proc/device-tree/compatible"], timeout
            )
            # device-tree compatible 是 \0 分隔的多串，替换为空格后折成一行。
            joined_dt = " ".join(dt_out.replace("\x00", " ").split())
            # 只认含 "xxx,yyy" 逗号对的 compatible 串；挡登录 shell 横幅垃圾。
            board_dt = joined_dt if re.search(r"[a-z0-9]+,[a-z0-9]+", joined_dt) else None

            _, fw_out = _run(["-s", address, "shell", "cat /etc/fw_version"], timeout)
            firmware_version = _first_version(fw_out)
            if firmware_version is None:
                _, ver_out = _run(["-s", address, "shell", "cat /etc/version"], timeout)
                firmware_version = _first_version(ver_out)

            # v0.31：工程配置指纹（新构建固件才有，老固件读空 -> 跳过）
            _, bi_out = _run(["-s", address, "shell", "cat /etc/build-info"], timeout)
            build_info = _parse_kv(bi_out)
            _, cfg_out = _run(
                ["-s", address, "shell", _ENG_GREP], max(timeout, 10)
            )
            eng_config = _parse_eng_config(cfg_out)
            # product_name 缺失时用工程配置兜底（新固件可能不再带 /etc/product_name）
            if product_name is None:
                product_name = (
                    (build_info.get("project") or "").strip()
                    or (eng_config.get("product") or "").strip()
                    or (eng_config.get("project_type") or "").strip()
                    or None
                )

            # v0.23：读 /proc/cpuinfo 判定浮点 ABI（Features 含 vfp/neon 即硬浮点）。
            _, cpu_out = _run(["-s", address, "shell", "cat /proc/cpuinfo"], timeout)
            features, cpu_part, cpu_implementer, has_fpu = _parse_cpuinfo(cpu_out)
        finally:
            _run(["disconnect", address], timeout)

        # 固定 mac：eth0 优先，否则 wlan0，都没有则 None（fe: 前缀不再排除）
        if _is_fixed(eth_mac):
            fixed_mac, mac_source = eth_mac, "eth0"
        elif _is_fixed(wlan_mac):
            fixed_mac, mac_source = wlan_mac, "wlan0"
        else:
            fixed_mac, mac_source = None, None

        # v0.28.2：判据只看「adb 连得上 + shell 能执行」；元数据缺失不影响入池
        level = HealthLevel.L3 if shell_ok else HealthLevel.L0
        return self._result(
            ProbeResult, level, fixed_mac, product_name, eth_mac, wlan_mac,
            storage_type, has_wifi, board_dt, firmware_version,
            features, cpu_part, cpu_implementer, has_fpu, shell_ok=shell_ok,
            mac_source=mac_source, build_info=build_info, eng_config=eng_config,
        )

    @staticmethod
    def _result(
        ProbeResult, level, fixed_mac, product_name, eth_mac, wlan_mac,
        storage_type, has_wifi, board_dt, firmware_version,
        features=None, cpu_part=None, cpu_implementer=None, has_fpu=False, shell_ok=False,
        mac_source=None, build_info=None, eng_config=None,
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
                "mac_source": mac_source,
                "mac_is_local_admin": bool(fixed_mac) and fixed_mac.lower().startswith("fe:"),
                "shell_ok": shell_ok,
                "storage_type": storage_type,
                "has_wifi": has_wifi,
                "board_dt": board_dt,
                "firmware_version": firmware_version,
                # v0.23：芯片/浮点能力，供截图二进制选择（vxosdbg=hard / xosdbg=soft）。
                "features": features,
                "cpu_part": cpu_part,
                "cpu_implementer": cpu_implementer,
                "has_fpu": has_fpu,
                # v0.31：工程配置指纹（高精度）。老固件无这两个文件时为 None。
                "build_info": build_info or None,
                "eng_config": eng_config or None,
                "config_product": (eng_config or {}).get("product"),
                "qm_version": (eng_config or {}).get("qm_version"),
                "hw_version": (eng_config or {}).get("hw_version"),
                "platform": (build_info or {}).get("platform"),
                "built_at": (build_info or {}).get("built_at"),
                "project_git": (build_info or {}).get("project_git"),
                "config_sha256": (build_info or {}).get("config_sha256"),
                "has_lcd": (eng_config or {}).get("has_lcd"),
                "has_touch": (eng_config or {}).get("has_touch"),
                "has_ethernet": (eng_config or {}).get("has_ethernet"),
                "cfg_has_wifi": (eng_config or {}).get("cfg_has_wifi"),
                "wifi_chip": (eng_config or {}).get("wifi_chip"),
            },
        )
