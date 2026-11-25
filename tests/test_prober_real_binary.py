"""v0.52.1：real probe 对二进制 stdout 安全解码，不因 UnicodeDecodeError 崩溃。

不依赖真实 adb / 真实设备网络。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.constants import HealthLevel
from app.domain import prober_real


def test_run_decodes_binary_stdout_without_raising(monkeypatch):
    """subprocess 返回 0xff 字节时，_run 不抛异常且 stdout 非空可安全处理。"""

    def fake_subprocess_run(*args, **kwargs):
        assert kwargs.get("text") is False or kwargs.get("text") is None
        assert kwargs.get("universal_newlines") is not True
        return SimpleNamespace(returncode=0, stdout=b"\xff\xff\xff", stderr=b"")

    monkeypatch.setattr(prober_real.subprocess, "run", fake_subprocess_run)
    rc, out = prober_real._run(["shell", "cat /proc/device-tree/compatible"], timeout=5)
    assert rc == 0
    assert out  # 非空（替换字符）
    assert "\ufffd" in out or len(out) >= 1
    # 再 strip / replace 也不应抛
    assert out.strip() == out
    assert out.replace("\x00", " ") is not None


def test_probe_l3_when_device_tree_is_binary(monkeypatch):
    """shell_ok=True 且 device-tree 为全 0xff 时，probe 仍返回 L3，不崩溃。"""
    monkeypatch.setattr(prober_real, "tcp_port_open", lambda *a, **k: True)

    def fake_run(cmd, timeout=5):
        joined = " ".join(cmd)
        if cmd[0] == "connect":
            return 0, "connected to 172.16.20.138:5555"
        if cmd[0] == "disconnect":
            return 0, ""
        if "echo __TRES_SHELL_OK__" in joined:
            return 0, "__TRES_SHELL_OK__"
        if "device-tree/compatible" in joined:
            # 模拟 _run 已安全解码后的替换字符串（与真实 47×0xff 路径一致）
            return 0, "\ufffd" * 47
        if "product_name" in joined:
            return 0, "esl2"
        return 0, ""

    monkeypatch.setattr(prober_real, "_run", fake_run)
    result = prober_real.RealXosProber().probe("172.16.20.138", 5555, 5)
    assert result.level == HealthLevel.L3
    assert result.extra["shell_ok"] is True
    assert result.extra.get("board_dt") is None  # 全替换字符不是合法 compatible


def test_run_via_subprocess_binary_device_tree_end_to_end(monkeypatch):
    """端到端：monkeypatch subprocess.run，device-tree 返回二进制，probe → L3。"""
    monkeypatch.setattr(prober_real, "tcp_port_open", lambda *a, **k: True)

    def fake_subprocess_run(full, capture_output=True, text=False, timeout=None, check=False):
        assert text is False
        joined = " ".join(str(x) for x in full)
        if "connect" in joined and "disconnect" not in joined:
            return SimpleNamespace(
                returncode=0, stdout=b"connected to 172.16.20.138:5555\n", stderr=b""
            )
        if "disconnect" in joined:
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        if "echo __TRES_SHELL_OK__" in joined:
            return SimpleNamespace(returncode=0, stdout=b"__TRES_SHELL_OK__\n", stderr=b"")
        if "device-tree/compatible" in joined:
            return SimpleNamespace(returncode=0, stdout=b"\xff" * 47, stderr=b"")
        if "product_name" in joined:
            return SimpleNamespace(returncode=0, stdout=b"esl2\n", stderr=b"")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(prober_real.subprocess, "run", fake_subprocess_run)
    result = prober_real.RealXosProber().probe("172.16.20.138", 5555, 5)
    assert result.level == HealthLevel.L3
    assert result.extra["shell_ok"] is True
    assert result.model == "esl2"
