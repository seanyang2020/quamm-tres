"""PowerRes 通道级「重启测试」runner：参数校验 / 互斥 / 失败即停 / stop 打断。

不碰真串口、真 DB：session 与 channel_action 全部用假实现。
"""
from __future__ import annotations

import time

import pytest

from app.domain import powerreboot
from app.domain.catalog import DomainError


class FakeSession:
    def __init__(self):
        self.added = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = False

    def add(self, obj):
        self.added.append(obj)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


@pytest.fixture(autouse=True)
def env(monkeypatch):
    powerreboot._reset_for_tests()
    sessions: list[FakeSession] = []

    def fake_new_session():
        s = FakeSession()
        sessions.append(s)
        return s

    monkeypatch.setattr(powerreboot, "_new_session", fake_new_session)
    yield sessions
    powerreboot._reset_for_tests()


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _fail(code: str, phase: str = "power_off", error: str = "boom") -> dict:
    return {
        "result": "failed",
        "error_code": code,
        "phase": phase,
        "error": error,
    }


def _ok_result():
    return {"result": "ok"}


def test_reject_bad_params():
    cases = [
        dict(node_id="", power_dev="p", channel=1, interval_s=5, repeat=1),
        dict(node_id="n", power_dev="", channel=1, interval_s=5, repeat=1),
        dict(node_id="n", power_dev="p", channel="x", interval_s=5, repeat=1),
        dict(node_id="n", power_dev="p", channel=1, interval_s=0, repeat=1),
        dict(node_id="n", power_dev="p", channel=1, interval_s=5, repeat=0),
        dict(node_id="n", power_dev="p", channel=1, interval_s=5, repeat=1001),
    ]
    for kwargs in cases:
        with pytest.raises(DomainError) as ei:
            powerreboot.start(**kwargs)
        assert ei.value.code == "BAD_PARAM"

    with pytest.raises(DomainError) as ei:
        powerreboot.start("n", "p", 1, 5, 1, hold_ms=-1)
    assert ei.value.code == "BAD_PARAM"


def test_too_long_rejected():
    with pytest.raises(DomainError) as ei:
        powerreboot.start("n", "p", 1, 100, 200, hold_ms=30000, confirm=True)
    assert ei.value.code == "TOO_LONG"
    assert ei.value.detail["estimated_s"] > 3600


def test_repeat_over_100_needs_confirm(monkeypatch):
    monkeypatch.setattr(
        powerreboot.powerres, "channel_action",
        lambda *a, **k: _ok_result(),
    )
    with pytest.raises(DomainError) as ei:
        powerreboot.start("n", "p", 1, 1, 101, hold_ms=0)
    assert ei.value.code == "NEED_CONFIRM"

    out = powerreboot.start("n", "p", 1, 1, 101, hold_ms=0, confirm=True)
    assert out["accepted"] is True
    powerreboot.stop()
    assert _wait_until(lambda: not powerreboot.status()["running"])


def test_singleton_mutex(monkeypatch):
    release = __import__("threading").Event()

    def slow_action(*args, **kwargs):
        release.wait(2.0)
        return _ok_result()

    monkeypatch.setattr(powerreboot.powerres, "channel_action", slow_action)
    powerreboot.start("n", "p", 1, 30, 2, hold_ms=0)

    with pytest.raises(DomainError) as ei:
        powerreboot.start("n2", "p2", 2, 5, 1)
    assert ei.value.code == "FLOW_BUSY"
    assert ei.value.detail["status"]["running"] is True

    release.set()
    powerreboot.stop()
    assert _wait_until(lambda: not powerreboot.status()["running"])


def test_failure_stops_immediately(monkeypatch):
    calls = []

    def failing_action(session, node_id, power_dev, channel, action, *, by, hold_ms=None):
        calls.append((node_id, power_dev, channel, action, hold_ms))
        return _fail("SERIAL_WRITE", phase="power_off", error="relay down")

    monkeypatch.setattr(powerreboot.powerres, "channel_action", failing_action)
    powerreboot.start("n", "p", 1, 1, 5, hold_ms=800)

    assert _wait_until(lambda: not powerreboot.status()["running"])
    snap = powerreboot.status()
    assert len(calls) == 1
    assert calls[0][3] == "power_cycle"
    assert calls[0][4] == 800
    assert snap["round"] == 1
    assert snap["last_result"] == "failed"
    assert "power_off" in snap["last_error"]
    assert "SERIAL_WRITE" in snap["last_error"]
    assert "relay down" in snap["last_error"]


def test_domain_error_stops(monkeypatch):
    def boom(*args, **kwargs):
        raise DomainError("POWERRES_BUSY", "通道被占用")

    monkeypatch.setattr(powerreboot.powerres, "channel_action", boom)
    powerreboot.start("n", "p", 1, 1, 3, hold_ms=0)
    assert _wait_until(lambda: not powerreboot.status()["running"])
    snap = powerreboot.status()
    assert snap["last_result"] == "failed"
    assert "POWERRES_BUSY" in snap["last_error"]
    assert "通道被占用" in snap["last_error"]


def test_stop_interrupts_wait(monkeypatch):
    calls = []

    def ok_action(*args, **kwargs):
        calls.append(1)
        return _ok_result()

    monkeypatch.setattr(powerreboot.powerres, "channel_action", ok_action)
    powerreboot.start("n", "p", 1, 30, 5, hold_ms=0)
    assert _wait_until(lambda: len(calls) == 1)

    t0 = time.monotonic()
    powerreboot.stop()
    assert _wait_until(lambda: not powerreboot.status()["running"], timeout=2.0)
    assert time.monotonic() - t0 < 5.0
    snap = powerreboot.status()
    assert snap["cancelled"] is True
    assert len(calls) == 1


def test_status_fields_and_completion(monkeypatch):
    calls = []

    def ok_action(*args, **kwargs):
        calls.append(1)
        return _ok_result()

    monkeypatch.setattr(powerreboot.powerres, "channel_action", ok_action)
    out = powerreboot.start("node-1", "relay1", 3, 1, 2, hold_ms=1200, by="web:u")
    assert out["kind"] == "powerres-reboot"
    assert out["accepted"] is True
    assert out["running"] is True
    assert out["node_id"] == "node-1"
    assert out["power_dev"] == "relay1"
    assert out["channel"] == 3
    assert out["interval_s"] == 1
    assert out["hold_ms"] == 1200
    assert out["total_rounds"] == 2
    assert out["started_at"]

    assert _wait_until(lambda: not powerreboot.status()["running"], timeout=6.0)
    snap = powerreboot.status()
    assert len(calls) == 2
    assert snap["round"] == 2
    assert snap["last_result"] == "ok"
    assert snap["last_error"] is None
    assert snap["cancelled"] is False
    assert isinstance(snap["elapsed_s"], int)
    assert snap["elapsed_s"] >= 0
