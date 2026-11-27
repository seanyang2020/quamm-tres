"""PowerRes 通道级「重启测试」：进程内单例循环 runner（跨节点全局互斥）。

每轮 = 断电 → 保持 hold_ms → 上电（复用 powerres.channel_action 的 power_cycle），
然后等待 interval_s；失败即停。线程内每轮新建 session，避免跨轮复用。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from app.db import get_session_factory
from app.domain import audit, powerres
from app.domain.catalog import DomainError
from app.domain.serial import _clamp_hold_ms

KIND = "powerres-reboot"
DEFAULT_HOLD_MS = 3000
# 单轮继电器帧（断/上电两次 TCP 写 + 应答）耗时上界，用于估算总时长
ROUND_FRAME_OVERHEAD_S = 2.0
MAX_ESTIMATED_S = 3600
CONFIRM_REPEAT_THRESHOLD = 100
REPEAT_MAX = 1000
INTERVAL_MIN_S = 1
INTERVAL_MAX_S = 3600

_LOCK = threading.Lock()
_STOP = threading.Event()
_STATE: dict = {}
_THREAD: threading.Thread | None = None


def _new_session():
    """每轮新建 session（测试可 monkeypatch 本函数避开真 DB）。"""
    return get_session_factory()()


def _empty_status() -> dict:
    return {
        "kind": KIND,
        "running": False,
        "node_id": "",
        "power_dev": "",
        "channel": None,
        "interval_s": None,
        "hold_ms": None,
        "round": 0,
        "total_rounds": 0,
        "started_at": None,
        "elapsed_s": 0,
        "last_result": None,
        "last_error": None,
        "cancelled": False,
    }


def _snapshot() -> dict:
    with _LOCK:
        return dict(_STATE) if _STATE else _empty_status()


def status() -> dict:
    """当前/最近一次运行状态快照。"""
    return {"ok": True, **_snapshot()}


def _bad(message: str) -> DomainError:
    return DomainError("BAD_PARAM", message)


def _parse_int(value, name: str, *, minimum: int, maximum: int) -> int:
    if value is None or isinstance(value, bool):
        raise _bad(f"{name} 必须为整数")
    try:
        v = int(value)
    except (TypeError, ValueError) as e:
        raise _bad(f"非法 {name}: {value!r}") from e
    if v < minimum or v > maximum:
        raise _bad(f"{name} 越界: {v}（允许 {minimum}..{maximum}）")
    return v


def _parse_hold_ms(value) -> int:
    if value is None or value is False or isinstance(value, bool):
        return DEFAULT_HOLD_MS
    try:
        v = int(value)
    except (TypeError, ValueError) as e:
        raise _bad(f"非法 hold_ms: {value!r}") from e
    if v < 0:
        raise _bad(f"hold_ms 不能为负: {v}")
    return _clamp_hold_ms(v)


def _readable_error(exc_or_result) -> str:
    """用 error/error_code/phase 拼可读文案。"""
    if isinstance(exc_or_result, DomainError):
        code = exc_or_result.code or "FAILED"
        msg = exc_or_result.message or ""
        return f"{code}: {msg}" if msg else code
    d = exc_or_result or {}
    code = d.get("error_code") or "FAILED"
    phase = d.get("phase") or ""
    msg = d.get("error") or d.get("message") or ""
    parts = []
    if phase:
        parts.append(f"{phase}")
    parts.append(str(code))
    if msg:
        parts.append(str(msg))
    return ": ".join(parts)


def _audit_start(by: str, state: dict) -> None:
    """开跑记一条审计；失败不影响主流程。"""
    session = None
    try:
        session = _new_session()
        audit.record(
            session,
            actor=by,
            action="powerres_reboot_test",
            detail={
                "node_id": state["node_id"],
                "power_dev": state["power_dev"],
                "channel": state["channel"],
                "interval_s": state["interval_s"],
                "hold_ms": state["hold_ms"],
                "total_rounds": state["total_rounds"],
            },
        )
        session.commit()
    except Exception:  # noqa: BLE001 - 审计失败不阻断测试
        try:
            if session is not None:
                session.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:  # noqa: BLE001
            pass


def _run(state: dict, interval_s: int, total_rounds: int, hold_ms: int,
         by: str) -> None:
    global _THREAD
    t0 = time.monotonic()
    last_result = None
    last_error = None
    try:
        for r in range(1, total_rounds + 1):
            if _STOP.is_set():
                break
            with _LOCK:
                _STATE["round"] = r

            session = None
            try:
                session = _new_session()
                result = powerres.channel_action(
                    session,
                    state["node_id"],
                    state["power_dev"],
                    state["channel"],
                    "power_cycle",
                    by=by,
                    hold_ms=hold_ms,
                )
            except DomainError as e:
                last_result = "failed"
                last_error = _readable_error(e)
                break
            except Exception as e:  # noqa: BLE001 - 线程内兜底，失败即停
                last_result = "failed"
                last_error = f"{type(e).__name__}: {e}"
                break
            finally:
                try:
                    if session is not None:
                        session.close()
                except Exception:  # noqa: BLE001
                    pass

            ok = isinstance(result, dict) and result.get("result") == "ok"
            with _LOCK:
                _STATE["last_result"] = result.get("result") if isinstance(result, dict) else None
                _STATE["round"] = r
                _STATE["elapsed_s"] = int(time.monotonic() - t0)
            if not ok:
                last_result = "failed"
                last_error = _readable_error(result)
                break
            last_result = "ok"

            if r < total_rounds and _STOP.wait(interval_s):
                break
    finally:
        with _LOCK:
            _STATE["running"] = False
            _STATE["elapsed_s"] = int(time.monotonic() - t0)
            _STATE["last_result"] = last_result
            _STATE["last_error"] = last_error
            _STATE["cancelled"] = bool(_STOP.is_set())
            _THREAD = None


def start(
    node_id,
    power_dev,
    channel,
    interval_s,
    repeat,
    hold_ms=None,
    by="",
    confirm: bool = False,
) -> dict:
    """启动重启测试；校验失败抛 DomainError（BAD_PARAM/NEED_CONFIRM/TOO_LONG/FLOW_BUSY）。"""
    global _THREAD

    node_id = str(node_id or "").strip()
    if not node_id:
        raise _bad("node_id 不能为空")
    power_dev = str(power_dev or "").strip()
    if not power_dev:
        raise _bad("power_dev 不能为空")
    try:
        channel = int(channel)
    except (TypeError, ValueError) as e:
        raise _bad(f"非法 channel: {channel!r}") from e

    interval = _parse_int(
        interval_s, "interval_s", minimum=INTERVAL_MIN_S, maximum=INTERVAL_MAX_S,
    )
    total_rounds = _parse_int(repeat, "repeat", minimum=1, maximum=REPEAT_MAX)
    hold = _parse_hold_ms(hold_ms)

    estimated = total_rounds * (hold / 1000.0 + ROUND_FRAME_OVERHEAD_S + interval)
    if estimated > MAX_ESTIMATED_S:
        raise DomainError(
            "TOO_LONG",
            f"预计耗时 {int(estimated)}s 超过上限 {MAX_ESTIMATED_S}s",
            {"estimated_s": int(estimated)},
        )
    if total_rounds > CONFIRM_REPEAT_THRESHOLD and not confirm:
        raise DomainError(
            "NEED_CONFIRM",
            f"次数 {total_rounds} 超过 {CONFIRM_REPEAT_THRESHOLD}，需确认",
            {"repeat": total_rounds},
        )

    with _LOCK:
        if _STATE.get("running"):
            raise DomainError(
                "FLOW_BUSY",
                "已有重启测试在运行",
                {"status": dict(_STATE)},
            )
        _STOP.clear()
        _STATE.clear()
        _STATE.update({
            "kind": KIND,
            "running": True,
            "node_id": node_id,
            "power_dev": power_dev,
            "channel": channel,
            "interval_s": interval,
            "hold_ms": hold,
            "round": 0,
            "total_rounds": total_rounds,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_s": 0,
            "last_result": None,
            "last_error": None,
            "cancelled": False,
        })
        state = dict(_STATE)
        _THREAD = threading.Thread(
            target=_run,
            args=(state, interval, total_rounds, hold, by),
            name="powerres-reboot",
            daemon=True,
        )
        _THREAD.start()

    _audit_start(by, state)
    return {"ok": True, "accepted": True, **state}


def stop() -> dict:
    """请求停止；等待中的间隔会被立即打断。"""
    with _LOCK:
        if _STATE.get("running"):
            _STOP.set()
            _STATE["cancelled"] = True
        snap = dict(_STATE) if _STATE else _empty_status()
    return {"ok": True, **snap}


def _reset_for_tests() -> None:
    """测试辅助：停掉线程并清空单例状态（不对外暴露路由）。"""
    global _THREAD
    _STOP.set()
    thread = _THREAD
    if thread is not None:
        thread.join(timeout=2.0)
    with _LOCK:
        _STOP.clear()
        _STATE.clear()
        _THREAD = None
