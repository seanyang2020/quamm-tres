"""设备发现（集成 adbscan 扫描能力的精简后端实现）。

与 MCP 设备注册保持同一套验证与信息要求：
- 先并发 TCP 扫描网段内 5555 端口（adbscan 口径：线程池 + 短超时）；
- 对每个命中主机调用 catalog.device_probe，生成与 MCP 一致的注册表单（form_id），
  并回读 product_name / MAC / 健康等级 / 是否已登记；
- 后续确认注册仍走 catalog.device_register，复用必填字段、机型字典等校验。

纯内存、只读探测，不 push / 刷机。
"""
from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from app.domain import catalog
from app.utils import utcnow

DEFAULT_PORT = 5555
DEFAULT_TIMEOUT = 1.0
DEFAULT_THREADS = 80
_SKIP_IFACE_PREFIXES = ("lo", "docker", "veth", "br-", "virbr", "sit", "wl-")


def active_networks() -> list[str]:
    """枚举本机活动网卡对应的 /24 网段（Linux /sys/class/net），失败回退默认路由。"""
    nets: list[str] = []
    try:
        import os

        root = "/sys/class/net"
        for iface in sorted(os.listdir(root)):
            if any(iface.startswith(p) for p in _SKIP_IFACE_PREFIXES):
                continue
            try:
                state = open(os.path.join(root, iface, "operstate")).read().strip()
                if state != "up":
                    continue
                ip = open(os.path.join(root, iface, "address")).read()  # placeholder
            except OSError:
                continue
            addr = _iface_ipv4(iface)
            if addr and not addr.startswith("127."):
                net = str(ipaddress.IPv4Network(f"{addr}/24", strict=False))
                if net not in nets:
                    nets.append(net)
    except OSError:
        pass

    if not nets:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            addr = s.getsockname()[0]
            s.close()
            nets.append(str(ipaddress.IPv4Network(f"{addr}/24", strict=False)))
        except OSError:
            pass
    return nets


def _iface_ipv4(iface: str) -> Optional[str]:
    import fcntl
    import struct

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(
            fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))[20:24]
        )
    except OSError:
        return None


def _arp_mac(ip: str) -> Optional[str]:
    """从 /proc/net/arp 读取 MAC（刚建立连接，缓存通常已更新）。"""
    try:
        for line in open("/proc/net/arp"):
            parts = line.split()
            if len(parts) >= 4 and parts[0] == ip:
                mac = parts[3].lower()
                if mac and mac != "00:00:00:00:00:00":
                    return mac
    except OSError:
        pass
    return None


def _port_open(ip: str, port: int, timeout: float) -> Optional[str]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        if sock.connect_ex((ip, port)) == 0:
            return ip
    finally:
        sock.close()
    return None


def _all_hosts(networks: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for spec in networks:
        net = ipaddress.IPv4Network(spec, strict=False)
        for host in net.hosts():
            out.append((str(host), spec))
    return out


def scan(session, networks: Optional[list[str]] = None, port: int = DEFAULT_PORT,
         timeout: float = DEFAULT_TIMEOUT, threads: int = DEFAULT_THREADS) -> dict:
    """扫描网段并对命中设备生成注册表单。返回结构化发现结果。"""
    nets = [n for n in (networks or []) if n] or active_networks()
    hosts = _all_hosts(nets)
    found: list[str] = []
    with ThreadPoolExecutor(max_workers=threads) as ex:
        for res in ex.map(lambda h: _port_open(h[0], port, timeout), hosts):
            if res and res not in found:
                found.append(res)
    found.sort(key=lambda x: tuple(int(p) for p in x.split(".")))

    devices = []
    for ip in found:
        rec = {
            "ip": ip,
            "port": port,
            "mac": _arp_mac(ip),
            "form_id": None,
            "probe": None,
            "other_info": None,
            "identity_match": None,
            "already_registered": False,
            "error": None,
        }
        try:
            # 给足单设备探测时间（默认 8s），避免并发扫描后 adb 读取被饿死导致机型为空。
            # 设备发现扫的是真实网络 ADB 设备，强制 real 后端，不受全局 mock 影响。
            probed = catalog.device_probe(
                session, ip=ip, port=port, agent_info="web-console",
                source="real", timeout=8,
            )
            rec["form_id"] = probed.get("form_id")
            rec["probe"] = probed.get("probe")
            rec["other_info"] = _build_other_info(probed.get("probe"))
            rec["identity_match"] = probed.get("identity_match")
            rec["already_registered"] = (
                (probed.get("identity_match") or {}).get("result") == "existing"
            )
        except catalog.DomainError as e:
            rec["error"] = e.message
        devices.append(rec)

    return {
        "scanned_at": utcnow().isoformat(),
        "networks": nets,
        "total_hosts": len(hosts),
        "found": len(found),
        "devices": devices,
    }


def _build_other_info(probe: dict | None) -> dict:
    """汇总综合探测信号，机型无法确定时供用户决策。"""
    if not probe:
        return {}
    extra = probe.get("extra") or {}
    return {
        "level": probe.get("level"),
        "product_name": extra.get("product_name") or probe.get("model"),
        "firmware": extra.get("firmware_version") or probe.get("build_fingerprint"),
        "eth_mac": extra.get("eth_mac"),
        "wlan_mac": extra.get("wlan_mac"),
        "storage": extra.get("storage_type"),
        "has_wifi": extra.get("has_wifi"),
        "board": extra.get("board_dt"),
    }

