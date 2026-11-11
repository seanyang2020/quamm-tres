"""设备层串口执行器（raw TCP 直连 tres-logs 串口服务器 / 本机 COM）。

本模块只干一件事：把一串「命令 + 延时 + 期望回包」按顺序发出去、收回来。
不碰数据库、不碰权限——那是 app/domain/serial.py 的事。

与 quamm-serial skill 共用同一套 steps 语义：

    {"data": "A0 01 01 A2", "encoding": "hex", "delay_ms": 200,
     "expect": null, "expect_mode": "none", "timeout_ms": 300}

expect_mode: none（只发不等）| contains | regex | exact
"""
from __future__ import annotations

import re
import socket
import threading
import time
from dataclasses import dataclass

DEFAULT_CONNECT_TIMEOUT_S = 3.0
DEFAULT_HARD_TIMEOUT_S = 5.0
MAX_HARD_TIMEOUT_S = 30.0
READ_SLICE_S = 0.05

VALID_EXPECT_MODES = ("none", "contains", "regex", "exact")
VALID_ENCODINGS = ("hex", "text")


# ---------------------------------------------------------------- 错误

class SerialError(Exception):
    """串口层错误基类（domain 层会翻成 DomainError）。"""

    code = "SERIAL_ERROR"

    def __init__(self, message: str, detail: dict | None = None):
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


class SerialBusy(SerialError):
    """端口被别的 holder 占着。"""
    code = "SERIAL_BUSY"


class SerialConfigError(SerialError):
    """配置不合法（endpoint/step 字段）。"""
    code = "SERIAL_CONFIG"


class SerialConnectError(SerialError):
    """连不上（机器断电 / 端口没映射 / 网络不通）。"""
    code = "SERIAL_CONNECT"


# ---------------------------------------------------------------- 编码

_HEX_CLEAN = re.compile(r"[^0-9a-fA-F]")


def encode_data(data: str, encoding: str = "hex") -> bytes:
    """把 step.data 变成真实字节。"""
    if encoding not in VALID_ENCODINGS:
        raise SerialConfigError(f"未知 encoding: {encoding}（只支持 hex/text）")
    raw = "" if data is None else str(data)
    if encoding == "hex":
        cleaned = _HEX_CLEAN.sub("", raw.replace("0x", "").replace("0X", ""))
        if len(cleaned) % 2:
            raise SerialConfigError(f"hex 命令字节数不成对: {raw!r}")
        try:
            return bytes.fromhex(cleaned)
        except ValueError as e:
            raise SerialConfigError(f"hex 命令解析失败: {raw!r}") from e
    # text：允许写 "\r\n" 这种字面转义
    cooked = (
        raw.replace("\\r", "\r")
        .replace("\\n", "\n")
        .replace("\\t", "\t")
        .replace("\\0", "\0")
    )
    return cooked.encode("utf-8")


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """tcp://172.16.10.77:4001 或 172.16.10.77:4001 -> (host, port)。"""
    s = str(endpoint or "").strip()
    if not s:
        raise SerialConfigError("endpoint 为空")
    low = s.lower()
    if low.startswith("tcp://"):
        s = s[6:]
    elif low.startswith("telnet://"):
        s = s[9:]
    host, sep, port = s.rpartition(":")
    if not sep or not host:
        raise SerialConfigError(f"endpoint 需要 host:port 形式: {endpoint!r}")
    try:
        p = int(port)
    except ValueError as e:
        raise SerialConfigError(f"endpoint 端口不是数字: {endpoint!r}") from e
    if not (1 <= p <= 65535):
        raise SerialConfigError(f"endpoint 端口越界: {p}")
    return host, p


# ---------------------------------------------------------------- 占用（进程内）

_holds: dict[str, dict] = {}
_port_locks: dict[str, threading.Lock] = {}
_hold_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _hold_guard:
        lk = _port_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _port_locks[key] = lk
        return lk


def acquire(key: str, holder: str, ttl_sec: float = 120.0) -> dict:
    """占住一路串口。同一 key 同时只允许一个 holder；不同 holder 抛 SerialBusy。"""
    now = time.monotonic()
    with _hold_guard:
        cur = _holds.get(key)
        if cur and cur["until"] > now and cur["holder"] != holder:
            left = int((cur["until"] - now) * 1000)
            raise SerialBusy(
                f"串口正被 {cur['holder']} 占用",
                {"holder": cur["holder"], "left_ms": left},
            )
        ttl = max(1.0, min(float(ttl_sec or 120.0), 600.0))
        info = {"holder": holder, "until": now + ttl, "ttl_sec": ttl}
        _holds[key] = info
        return {"key": key, "holder": holder, "ttl_sec": ttl}


def release(key: str, holder: str | None = None) -> bool:
    with _hold_guard:
        cur = _holds.get(key)
        if not cur:
            return False
        if holder is not None and cur["holder"] != holder:
            return False
        _holds.pop(key, None)
        return True


def holder_of(key: str) -> dict | None:
    now = time.monotonic()
    with _hold_guard:
        cur = _holds.get(key)
        if not cur:
            return None
        if cur["until"] <= now:
            _holds.pop(key, None)
            return None
        return {
            "holder": cur["holder"],
            "left_ms": int((cur["until"] - now) * 1000),
            "ttl_sec": cur["ttl_sec"],
        }


def force_release(key: str) -> bool:
    with _hold_guard:
        return bool(_holds.pop(key, None))


# ---------------------------------------------------------------- 连接

def _open(target: tuple[str, str], baudrate: int = 115200, connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S):
    kind, value = target
    if kind == "tcp":
        host, port = parse_endpoint(value)
        try:
            conn = socket.create_connection((host, port), timeout=connect_timeout)
        except OSError as e:
            raise SerialConnectError(
                f"连不上串口服务器 {host}:{port}（{e}）",
                {"host": host, "port": port},
            ) from e
        conn.settimeout(READ_SLICE_S)
        return conn, "tcp"
    if kind == "local":
        try:
            import serial as pyserial  # type: ignore
        except ImportError as e:  # pragma: no cover - 依赖可选
            raise SerialConfigError(
                "本机串口（transport=local）需要 pyserial，当前环境未安装"
            ) from e
        try:
            return (
                pyserial.Serial(value, baudrate=baudrate, timeout=READ_SLICE_S),
                "local",
            )
        except Exception as e:
            raise SerialConnectError(f"打开本机串口 {value} 失败（{e}）") from e
    raise SerialConfigError(f"未知 transport: {kind}")


def _close(conn, kind: str) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _read_slice(conn, kind: str) -> bytes:
    try:
        if kind == "tcp":
            return conn.recv(4096)
        return conn.read(4096) or b""
    except (socket.timeout, TimeoutError):
        return b""
    except OSError:
        return b""


def can_match(text: str, expect: str | None, mode: str) -> bool:
    if mode == "none":
        return True
    if mode not in VALID_EXPECT_MODES:
        raise SerialConfigError(f"未知 expect_mode: {mode}")
    if not expect:
        return True
    if mode == "contains":
        return expect in text
    if mode == "exact":
        return text.strip() == expect.strip()
    try:
        return bool(re.search(expect, text))
    except re.error as e:
        raise SerialConfigError(f"expect 正则非法: {expect!r}（{e}）") from e


# ---------------------------------------------------------------- 结果

@dataclass
class StepResult:
    index: int
    data: str
    encoding: str
    bytes_sent: int
    response: str
    matched: bool
    duration_ms: int
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "data": self.data,
            "encoding": self.encoding,
            "bytes_sent": self.bytes_sent,
            "response": self.response,
            "matched": self.matched,
            "duration_ms": self.duration_ms,
            "error": self.error,
        }


@dataclass
class ActionResult:
    ok: bool
    transport: str
    target: str
    steps: list[StepResult]
    response_tail: str
    duration_ms: int
    error: str | None = None
    error_code: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "transport": self.transport,
            "target": self.target,
            "steps": [s.to_dict() for s in self.steps],
            "response_tail": self.response_tail,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "error_code": self.error_code,
        }


# ---------------------------------------------------------------- 执行

def normalize_steps(steps) -> list[dict]:
    if not isinstance(steps, list) or not steps:
        raise SerialConfigError("steps 必须是非空数组")
    out = []
    for i, raw in enumerate(steps):
        if not isinstance(raw, dict):
            raise SerialConfigError(f"第 {i + 1} 步不是对象")
        data = raw.get("data")
        if data is None or str(data) == "":
            raise SerialConfigError(f"第 {i + 1} 步缺少 data")
        mode = str(raw.get("expect_mode") or "none").lower()
        if mode not in VALID_EXPECT_MODES:
            raise SerialConfigError(f"第 {i + 1} 步 expect_mode 非法: {mode}")
        enc = str(raw.get("encoding") or "").lower() or None
        if enc and enc not in VALID_ENCODINGS:
            raise SerialConfigError(f"第 {i + 1} 步 encoding 非法: {enc}")
        out.append(
            {
                "data": data,
                "encoding": enc,
                "delay_ms": max(0, int(raw.get("delay_ms") or 0)),
                "expect": raw.get("expect"),
                "expect_mode": mode,
                "timeout_ms": max(0, int(raw.get("timeout_ms") or 0)),
            }
        )
    return out


def execute_steps(
    target: tuple[str, str],
    steps,
    *,
    default_encoding: str = "hex",
    baudrate: int = 115200,
    hard_timeout_s: float = DEFAULT_HARD_TIMEOUT_S,
) -> ActionResult:
    """按顺序执行 steps，返回逐步结果。整个过程受 hard_timeout_s 硬顶约束。"""
    steps = normalize_steps(steps)
    hard = max(0.5, min(float(hard_timeout_s or DEFAULT_HARD_TIMEOUT_S), MAX_HARD_TIMEOUT_S))
    kind, value = target
    started = time.monotonic()
    results: list[StepResult] = []
    tail: list[str] = []
    conn = None
    try:
        conn, real_kind = _open(target, baudrate=baudrate)
    except SerialError as e:
        return ActionResult(
            ok=False, transport=kind, target=value, steps=[], response_tail="",
            duration_ms=int((time.monotonic() - started) * 1000),
            error=e.message, error_code=e.code,
        )

    try:
        for idx, st in enumerate(steps):
            if time.monotonic() - started > hard:
                results.append(
                    StepResult(idx, st["data"], st["encoding"] or default_encoding, 0, "",
                               False, 0, f"超过整体硬超时 {hard}s")
                )
                break
            t0 = time.monotonic()
            enc = st["encoding"] or default_encoding
            try:
                payload = encode_data(st["data"], enc)
            except SerialError as e:
                results.append(
                    StepResult(idx, st["data"], enc, 0, "", False,
                               int((time.monotonic() - t0) * 1000), e.message)
                )
                return ActionResult(
                    ok=False, transport=real_kind, target=value, steps=results,
                    response_tail="".join(tail),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=e.message, error_code=e.code,
                )
            write_err = None
            try:
                if real_kind == "tcp":
                    conn.sendall(payload)
                else:
                    conn.write(payload)
            except OSError as e:
                write_err = f"写入失败（{e}）"

            if st["delay_ms"]:
                time.sleep(min(st["delay_ms"] / 1000.0, hard))

            resp = b""
            matched = True
            if st["expect_mode"] != "none" and write_err is None:
                budget = st["timeout_ms"] or 300
                deadline = time.monotonic() + min(budget / 1000.0, hard)
                text_so_far = ""
                while True:
                    chunk = _read_slice(conn, real_kind)
                    if chunk:
                        resp += chunk
                        text_so_far = resp.decode("utf-8", errors="replace")
                        try:
                            if can_match(text_so_far, st["expect"], st["expect_mode"]):
                                matched = True
                                break
                        except SerialError:
                            raise
                    if time.monotonic() >= deadline:
                        matched = False
                        break
                    if not chunk:
                        time.sleep(0.01)
            elif st["expect_mode"] == "none":
                # 只发不等，但把已经在缓冲里的回包收一下（不打乱下一步）
                for _ in range(3):
                    chunk = _read_slice(conn, real_kind)
                    if not chunk:
                        break
                    resp += chunk

            text = resp.decode("utf-8", errors="replace")
            if text:
                tail.append(text)
                tail = tail[-3:]
            err = write_err
            if not matched and not err:
                err = f"未匹配到期望回包（{st['expect_mode']}: {st['expect']!r}）"
            results.append(
                StepResult(idx, st["data"], enc, len(payload), text, matched,
                           int((time.monotonic() - t0) * 1000), err)
            )
            if err:
                return ActionResult(
                    ok=False, transport=real_kind, target=value, steps=results,
                    response_tail="".join(tail),
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error=err,
                    error_code="SERIAL_TIMEOUT" if "未匹配" in err else "SERIAL_WRITE",
                )
        return ActionResult(
            ok=True, transport=real_kind, target=value, steps=results,
            response_tail="".join(tail),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    finally:
        _close(conn, real_kind)


def read_stream(
    target: tuple[str, str],
    *,
    seconds: float = 3.0,
    max_bytes: int = 8192,
    baudrate: int = 115200,
) -> dict:
    """短读：连上、读 N 秒（或读满 max_bytes），返回内容。用于「测试」「看启动输出」。"""
    kind, value = target
    started = time.monotonic()
    try:
        conn, real_kind = _open(target, baudrate=baudrate)
    except SerialError as e:
        return {
            "ok": False, "transport": kind, "target": value, "data": "",
            "bytes": 0, "duration_ms": int((time.monotonic() - started) * 1000),
            "error": e.message, "error_code": e.code,
        }
    buf = b""
    deadline = time.monotonic() + max(0.1, min(float(seconds or 3.0), 30.0))
    try:
        while time.monotonic() < deadline and len(buf) < max_bytes:
            chunk = _read_slice(conn, real_kind)
            if chunk:
                buf += chunk
            else:
                time.sleep(0.01)
    finally:
        _close(conn, real_kind)
    return {
        "ok": True, "transport": real_kind, "target": value,
        "data": buf.decode("utf-8", errors="replace"),
        "bytes": len(buf),
        "duration_ms": int((time.monotonic() - started) * 1000),
        "error": None,
        "error_code": None,
    }


def probe_target(target: tuple[str, str], *, baudrate: int = 115200, seconds: float = 2.0) -> dict:
    """自检：能不能连上、N 秒内有没有字节。"""
    r = read_stream(target, seconds=seconds, baudrate=baudrate)
    r["connected"] = bool(r.get("ok"))
    r["had_bytes"] = bool(r.get("bytes"))
    return r
