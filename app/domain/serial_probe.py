"""串口手动探测：直连 TCP COM，读回 shell 提示符与身份信息（只读命令）。

用于“添加设备 → 串口（UART）”里对宿主已纳管、但指纹为空的一路串口做一次
主动核验。只发只读命令（cat / ifconfig / hostname），不写设备。
"""
from __future__ import annotations

import json
import re
import socket
import time
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.domain.catalog import DomainError
from app.models import WireLogNode, WireLogPortBinding
from app.utils import utcnow

_FP_CMD = (
    "echo __WL_FP__; "
    "for f in /etc/build-info /root/build-info /build-info; do [ -f \"$f\" ] && cat \"$f\"; done; "
    "cat /proc/device-tree/serial-number 2>/dev/null; echo; "
    "ifconfig 2>/dev/null; "
    "echo __WL_END__\n"
)


def _parse_endpoint(endpoint: str) -> tuple[str, int]:
    raw = (endpoint or "").strip()
    if raw.startswith("tcp://"):
        u = urlparse(raw)
    else:
        u = urlparse("tcp://" + raw)
    if not u.hostname or not u.port:
        raise DomainError("SERIAL_BAD_ENDPOINT", f"非法串口接入地址：{endpoint}")
    return u.hostname, int(u.port)


def _read_tail(sock: socket.socket, total_timeout: float, stop: bytes | None = None) -> str:
    chunks: list[bytes] = []
    deadline = time.monotonic() + total_timeout
    while time.monotonic() < deadline:
        try:
            data = sock.recv(4096)
        except socket.timeout:
            break
        if not data:
            break
        chunks.append(data)
        blob = b"".join(chunks)
        if stop and stop in blob:
            break
        if len(blob) > 65536:
            break
    return b"".join(chunks).decode("utf-8", "ignore")


def _norm_mac(value: str) -> str | None:
    v = value.strip().lower()
    if v in ("00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"):
        return None
    return v if re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", v) else None


def probe_binding(session: Session, node_id: str, slot: int, timeout: float = 3.0,
                  actor: str | None = None) -> dict:
    binding = (session.query(WireLogPortBinding)
               .filter_by(node_id=node_id, slot=int(slot)).one_or_none())
    if binding is None:
        raise DomainError("SERIAL_SLOT_NOT_FOUND", f"串口路不存在：{node_id}#{slot}")
    node = session.get(WireLogNode, node_id)
    if node is None or node.status != "online":
        raise DomainError("SERIAL_HOST_NOT_ONLINE", "串口节点不在线，无法手动探测")
    host, port = _parse_endpoint(binding.endpoint or "")
    started = time.monotonic()
    raw = ""
    prompt_seen = False
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(1.0)
            sock.sendall(b"\r\n")
            time.sleep(0.25)
            pre = _read_tail(sock, 0.8)
            raw += pre
            sock.sendall(_FP_CMD.encode("utf-8"))
            # 终端可能回显命令文本，不能拿哨兵做提前停止；按超时收口即可。
            out = _read_tail(sock, timeout)
            raw += out
    except (OSError, socket.timeout) as exc:
        raise DomainError("SERIAL_PROBE_FAILED", f"连接 {host}:{port} 失败：{exc}") from exc
    prompt_seen = any(p in raw for p in ("~ #", "# ", "$ ", "> "))

    macs = []
    for m in re.findall(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}", raw):
        nm = _norm_mac(m)
        if nm and nm not in macs:
            macs.append(nm)
    ips = []
    for m in re.findall(r"inet (?:addr:)?(\d+\.\d+\.\d+\.\d+)", raw):
        if m not in ("127.0.0.1", "0.0.0.0") and m not in ips:
            ips.append(m)
    sn_m = re.search(r"(?:sn|serial(?:no)?|serial-number)\s*[=:]\s*([A-Za-z0-9._:\-]+)",
                     raw, re.I)
    project_m = re.search(r"project\s*[=:]\s*([A-Za-z0-9._\-]+)", raw, re.I)
    model_m = re.search(r"model\s*[=:]\s*([A-Za-z0-9._\-]+)", raw, re.I)

    identity = {
        "mac": macs[0] if macs else None,
        "sn": sn_m.group(1) if sn_m else None,
        "ip": ips[0] if ips else None,
    }
    system = {
        "project": project_m.group(1) if project_m else None,
        "model": model_m.group(1) if model_m else None,
    }
    has_identity = any(identity.values())
    reachable = prompt_seen or bool(raw.strip())
    level = "L3" if reachable and has_identity else ("L2" if reachable else "L0")

    if identity["mac"]:
        binding.shell_mac = identity["mac"]
    if identity["ip"]:
        binding.shell_ip = identity["ip"]
    if level in ("L2", "L3"):
        binding.shell_reachable = True
        binding.health_state = "probed"
    else:
        binding.shell_reachable = False
        binding.health_state = "no_response"
    binding.probe_json = json.dumps({
        "verdict": "manual_probe",
        "identity": identity,
        "system": system,
        "probe": {
            "level": level,
            "prompt_seen": prompt_seen,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "at": utcnow().isoformat(),
            "actor": actor,
            "raw_tail": raw[-2000:],
        },
    }, ensure_ascii=False)[:8000]
    binding.last_checked_at = utcnow()
    session.commit()
    return {"level": level, "reachable": reachable,
            "prompt_seen": prompt_seen, "identity": identity,
            "system": system, "raw_tail": raw[-1000:]}
