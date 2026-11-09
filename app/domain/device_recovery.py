"""pending 设备自动恢复：端口敲门（port knock）拉起设备端 adbd。

背景：部分设备能 ping 通但 5555 无监听（adbd 未起 / 网络 ADB 未开）。设备端有守护
进程监听一组 TCP 敲门序列，命中后启动 adbd。本模块用标准库复现 adbscan 的敲门算法，
行为等价于 `adbscan -k -e <eventcode> <ip>`（默认事件码 2223），不依赖外部二进制。

敲门序列算法（与 adbscan.generate_knock_sequence 一致）：事件码逐位轮转，每位轮转值
作为一个端口；3 位码额外 +1000。例如 2223 → [2223, 2232, 2322, 3222]。
"""
from __future__ import annotations

import socket
import subprocess
import time

# 内存态：每台 IP 最近一次敲门时间戳，用于冷却，避免每轮巡检重复敲门。
_last_knock: dict[str, float] = {}
KNOCK_COOLDOWN_SECONDS = 600


def generate_knock_sequence(eventcode: str) -> list[int]:
    """由事件码生成端口敲门序列（逐位轮转）。"""
    if not eventcode.isdigit():
        raise ValueError(f"事件码必须为纯数字：{eventcode}")
    digits = len(eventcode)
    if digits < 3 or digits > 6:
        raise ValueError(f"事件码长度须为 3–6 位：{digits}")
    rotation_count = digits if digits <= 4 else 5
    sequence: list[int] = []
    current = eventcode
    for i in range(rotation_count):
        port = int(current) + 1000 if digits == 3 else int(current)
        sequence.append(port)
        if i < rotation_count - 1:
            current = current[1:] + current[0]
    return sequence


def ping_ok(host: str) -> bool:
    """ICMP 可达（Linux ping，单次、2s 超时）。"""
    try:
        proc = subprocess.run(
            ["ping", "-c", "1", "-W", "2", host],
            capture_output=True, timeout=6,
        )
        return proc.returncode == 0
    except Exception:
        return False


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """目标 TCP 端口是否已有服务监听。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _send_knock(host: str, ports: list[int], interval: float = 0.5) -> None:
    for i, port in enumerate(ports):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        # connect_ex 不抛异常：敲门本就是向关闭端口发起连接尝试。
        sock.connect_ex((host, port))
        sock.close()
        if i < len(ports) - 1:
            time.sleep(interval)


def knock_recover(
    host: str,
    eventcode: str,
    adb_port: int = 5555,
    wait_timeout: int = 30,
    use_cooldown: bool = True,
) -> dict:
    """尝试敲门拉起 adbd。

    返回 dict：
      - 端口已在监听：{already_up, adb_ready}
      - 冷却中：{skipped: "cooldown", adb_ready: False}
      - 敲门后 adb 起来：{recovered, adb_ready, sequence}
      - 超时未起：{recovered: False, adb_ready: False, sequence, error}
    """
    if tcp_open(host, adb_port):
        return {"already_up": True, "adb_ready": True}

    now = time.time()
    if use_cooldown and host in _last_knock and now - _last_knock[host] < KNOCK_COOLDOWN_SECONDS:
        return {"skipped": "cooldown", "adb_ready": False,
                "wait_seconds": int(KNOCK_COOLDOWN_SECONDS - (now - _last_knock[host]))}

    try:
        ports = generate_knock_sequence(eventcode)
    except ValueError as exc:
        return {"recovered": False, "adb_ready": False, "error": str(exc)}

    _last_knock[host] = now
    _send_knock(host, ports)

    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        if tcp_open(host, adb_port):
            return {"recovered": True, "adb_ready": True, "sequence": ports}
        time.sleep(1.0)
    return {"recovered": False, "adb_ready": False, "sequence": ports,
            "error": f"敲门后 {wait_timeout}s 内 adb 未就绪"}


def reset_cooldown() -> None:
    """测试用：清空冷却记录。"""
    _last_knock.clear()
