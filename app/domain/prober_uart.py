"""经 WireLog 串口（serialhub raw TCP）的 uart 探测（ADR-9）。

ADB 不可达时的兜底通道：经 ``wl_port_binding`` 找到设备所在日志机的
raw TCP（node.ip:9001，登录式 token），登录后 WRITE 拿 TTL 锁，向被测
设备 shell 写短命令并按定界块解析。不主动写则无回包，故必须写命令。

两种模式（周期/写入量分离）：
- keepalive（轻量）：仅 echo 探针，确认 shell 可达。
- full（一致性）：采集 MAC/IP/SN 全量身份。
"""
from __future__ import annotations

import logging
import socket
import time

from app.constants import HealthLevel
from app.domain.probe import ProbeResult
from app.models import WireLogNode, WireLogPortBinding

logger = logging.getLogger(__name__)

DEFAULT_RAW_TCP_PORT = 9001
CONNECT_TIMEOUT_S = 3.0
READ_SLICE_S = 0.1


class UartProber:
    def __init__(self, connect_timeout: float = CONNECT_TIMEOUT_S):
        self.connect_timeout = connect_timeout

    # -------------------------------------------------- 定位
    def _endpoint_for(self, session, device_id: str):
        binding = (session.query(WireLogPortBinding)
                   .filter_by(bound_device_id=device_id).first())
        if binding is None:
            return None
        node = session.get(WireLogNode, binding.node_id)
        if node is None or not node.ip:
            return None
        caps = node.caps_json if isinstance(node.caps_json, dict) else {}
        port = int(caps.get("raw_tcp_port") or DEFAULT_RAW_TCP_PORT)
        token = caps.get("local_token") or ""
        return {
            "host": node.ip, "port": port, "token": token,
            "slot": binding.slot, "tty": binding.tty_name,
        }

    # -------------------------------------------------- 低层收发
    def _read_until(self, conn, needle, timeout):
        buf = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                chunk = b""
            except OSError:
                break
            if chunk:
                buf += chunk
                if needle and needle.encode() in buf:
                    return buf.decode("utf-8", "replace")
            else:
                time.sleep(READ_SLICE_S)
        return buf.decode("utf-8", "replace")

    def _cmd(self, conn, text, wait=0.5):
        conn.sendall((text + "\n").encode())
        time.sleep(wait)

    # -------------------------------------------------- 主入口
    def probe_via_device(self, session, device_id, timeout=8, full=False):
        ep = self._endpoint_for(session, device_id)
        if ep is None:
            logger.debug("uart: no binding for %s", device_id)
            return ProbeResult(level=HealthLevel.L0, extra={"uart": "no_binding"})
        return self._probe(ep["host"], ep["port"], ep["token"], timeout, full)

    def probe(self, host, port=DEFAULT_RAW_TCP_PORT, timeout=8,
              token="", full=False):
        return self._probe(host, port, token, timeout, full)

    def _probe(self, host, port, token, timeout, full):
        conn = None
        try:
            conn = socket.create_connection((host, port), timeout=self.connect_timeout)
            conn.settimeout(READ_SLICE_S)
            self._read_until(conn, "login:", 2.0)
            conn.sendall((str(token) + "\n").encode())
            welcome = self._read_until(conn, "OK", 2.0)
            if "OK" not in welcome:
                return ProbeResult(level=HealthLevel.L0,
                                   extra={"uart": "login_failed"})
            # 拿 TTL 写锁，才能向 shell 写命令
            self._cmd(conn, "WRITE", wait=0.5)
            lock = self._read_until(conn, "WRITE OK", 2.0)
            if "WRITE OK" not in lock:
                return ProbeResult(level=HealthLevel.L0,
                                   extra={"uart": "lock_failed"})

            self._cmd(conn, "echo BEG", wait=0.3)
            if full:
                self._cmd(conn, "cat /sys/class/net/wlan0/address", wait=0.4)
                self._cmd(conn,
                          "ifconfig wlan0 | grep -o 'inet addr:[0-9.]*' | cut -d: -f2",
                          wait=0.6)
                self._cmd(conn, "cat /proc/cmdline", wait=0.5)
            else:
                self._cmd(conn, "echo __UART_OK__", wait=0.4)
            self._cmd(conn, "echo END", wait=0.4)
            block = self._read_until(conn, "END", max(1.0, timeout - 4))
        except (OSError, socket.timeout) as e:
            logger.debug("uart probe error %s:%s: %s", host, port, e)
            return ProbeResult(level=HealthLevel.L0, extra={"uart": str(e)})
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

        if "BEG" not in block or "END" not in block:
            return ProbeResult(level=HealthLevel.L0, extra={"uart": "no_block"})
        if not full:
            ok = "__UART_OK__" in block
            return ProbeResult(level=HealthLevel.L3 if ok else HealthLevel.L0,
                               extra={"uart_reachable": ok})

        mac = _first_match(block, _MAC_RE)
        ip = _first_non_local_ip(block)
        sn = _extract_sn(block)
        if mac is None and sn is None:
            return ProbeResult(level=HealthLevel.L0, extra={"uart": "no_identity"})
        return ProbeResult(
            level=HealthLevel.L3, mac=mac, sn=sn, model=None,
            extra={"uart_reachable": True, "shell_ip": ip},
        )


import re

_MAC_RE = re.compile(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}")
_IP_RE = re.compile(r"\d+(?:\.\d+){3}")
_SN_RE = re.compile(r"sn=([0-9A-Za-z_]+)")


def _first_match(text, rx):
    m = rx.search(text)
    return m.group(0) if m else None


def _first_non_local_ip(text):
    for m in _IP_RE.finditer(text):
        v = m.group(0)
        if not v.startswith("127.") and not v.endswith(".255"):
            return v
    return None


def _extract_sn(text):
    m = _SN_RE.search(text)
    return m.group(1) if m else None
