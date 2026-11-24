"""交互式 shell 回退：fake adb 模拟两种 adbd（忽略参数 / 正常执行）。"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from app.constants import HealthLevel
from app.domain import prober_real

FAKE_ADB = Path(__file__).resolve().parent / "fake_adb_interactive.py"


@pytest.fixture()
def fake_adb_wrapper(tmp_path, monkeypatch):
    """生成可执行 wrapper：把调用转发到 fake_adb_interactive.py。"""
    wrapper = tmp_path / "fake-adb"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'exec "{sys.executable}" "{FAKE_ADB}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(prober_real, "ADB_BIN", str(wrapper))
    monkeypatch.setattr(prober_real, "tcp_port_open", lambda *a, **k: True)
    return wrapper


def _probe(host="172.16.60.206"):
    return prober_real.RealXosProber().probe(host, 5555, 5)


def test_interactive_adbd_fallback_collects_fields(fake_adb_wrapper, monkeypatch):
    """变体 A：忽略 shell 参数 → 走交互回退，拿到 product/mac。"""
    monkeypatch.setenv("FAKE_ADB_MODE", "interactive")
    monkeypatch.setenv("FAKE_ADB_PRODUCT", "nxf7e")
    monkeypatch.setenv("FAKE_ADB_ETH_MAC", "")
    monkeypatch.setenv("FAKE_ADB_WLAN_MAC", "dc:0d:30:07:01:00")
    monkeypatch.setenv("TRES_INTERACTIVE_SHELL_FALLBACK", "1")
    # 子进程需继承 FAKE_ADB_*；monkeypatch.setenv 已写入 os.environ
    r = _probe()
    assert r.level == HealthLevel.L3
    assert r.extra["shell_ok"] is True
    assert r.extra["product_name"] == "nxf7e"
    assert r.mac == "dc:0d:30:07:01:00"
    assert r.extra["mac_source"] == "wlan0"
    assert r.extra["wlan_mac"] == "dc:0d:30:07:01:00"
    assert r.extra["has_wifi"] is True
    assert r.extra["storage_type"] == "nor"
    assert r.extra["has_fpu"] is True
    assert r.extra["cpu_part"] == "0xc07"


def test_normal_adbd_still_uses_cmd_shell(fake_adb_wrapper, monkeypatch):
    """变体 B：正常 shell <cmd> → 不走回退，字段正确。"""
    monkeypatch.setenv("FAKE_ADB_MODE", "normal")
    monkeypatch.setenv("FAKE_ADB_PRODUCT", "esl2")
    monkeypatch.setenv("FAKE_ADB_ETH_MAC", "0c:c1:19:90:0b:eb")
    monkeypatch.setenv("FAKE_ADB_WLAN_MAC", "fe:0f:e2:1e:1c:5f")
    monkeypatch.setenv("TRES_INTERACTIVE_SHELL_FALLBACK", "1")
    r = _probe("172.16.60.87")
    assert r.level == HealthLevel.L3
    assert r.extra["shell_ok"] is True
    assert r.extra["product_name"] == "esl2"
    assert r.mac == "0c:c1:19:90:0b:eb"
    assert r.extra["mac_source"] == "eth0"


def test_fallback_disabled_keeps_l0_on_interactive_adbd(fake_adb_wrapper, monkeypatch):
    """关闭回退开关：变体 A 与改动前一致，判 L0。"""
    monkeypatch.setenv("FAKE_ADB_MODE", "interactive")
    monkeypatch.setenv("FAKE_ADB_PRODUCT", "nxf7e")
    monkeypatch.setenv("FAKE_ADB_WLAN_MAC", "dc:0d:30:07:01:00")
    monkeypatch.setenv("TRES_INTERACTIVE_SHELL_FALLBACK", "0")
    r = _probe()
    assert r.level == HealthLevel.L0
    assert r.extra["shell_ok"] is False
    assert r.extra["product_name"] is None


def test_shell_dead_interactive_failure_is_l0(monkeypatch):
    """真死 shell：常规探针失败 + 交互回退也失败 → L0。"""
    monkeypatch.setattr(prober_real, "tcp_port_open", lambda *a, **k: True)
    monkeypatch.setenv("TRES_INTERACTIVE_SHELL_FALLBACK", "1")

    def fake_run(cmd, timeout=5):
        if cmd[0] == "connect":
            return 0, "connected to 172.16.60.55:5555"
        if cmd[0] == "disconnect":
            return 0, ""
        if "echo __TRES_SHELL_OK__" in " ".join(cmd):
            return 1, "adbd: exec '/bin/sh' failed"
        return 0, ""

    monkeypatch.setattr(prober_real, "_run", fake_run)
    monkeypatch.setattr(
        prober_real, "_run_interactive_shell", lambda *a, **k: (False, [])
    )
    r = _probe("172.16.60.55")
    assert r.level == HealthLevel.L0
    assert r.extra["shell_ok"] is False
