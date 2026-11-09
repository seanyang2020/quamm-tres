"""v0.28.2：真实探测器入池口径 —— adb 连得上且 shell 能执行即 L3。

用 monkeypatch 替换 prober_real._run，不依赖真实 adb / 真实设备。
"""
from __future__ import annotations

from app.constants import HealthLevel
from app.domain import prober_real


def _fake_factory(connect_out: str, shell_alive: bool, product: str | None = "esl2"):
    def fake_run(cmd, timeout=5):
        joined = " ".join(cmd)
        if cmd[0] == "connect":
            return 0, connect_out
        if cmd[0] == "disconnect":
            return 0, ""
        if "echo __TRES_SHELL_OK__" in joined:
            return (0, "__TRES_SHELL_OK__") if shell_alive else (1, "adbd: exec '/bin/sh' failed")
        if "product_name" in joined:
            return (0, product) if product else (0, "cat: can't open '/etc/product_name': No such file")
        if "eth0/address" in joined or "wlan0/address" in joined:
            return 0, "cat: can't open '/sys/class/net/eth0/address': No such file or directory"
        return 0, ""

    return fake_run


def test_shell_alive_is_l3_without_product_or_mac(monkeypatch):
    """设备没有 product_name、也没有固定 MAC，只要 shell 能执行就是 L3（esl2 场景）。"""
    monkeypatch.setattr(
        prober_real, "_run",
        _fake_factory("connected to 172.16.20.212:5555", True, product=None),
    )
    result = prober_real.RealXosProber().probe("172.16.20.212", 5555, 5)
    assert result.level == HealthLevel.L3
    assert result.reachable_l3 is True
    assert result.mac is None
    assert result.extra["shell_ok"] is True


def test_shell_alive_with_product_and_mac_is_l3(monkeypatch):
    monkeypatch.setattr(
        prober_real, "_run",
        _fake_factory("already connected to 10.0.0.5:5555", True, product="esl2"),
    )
    result = prober_real.RealXosProber().probe("10.0.0.5", 5555, 5)
    assert result.level == HealthLevel.L3
    assert result.model == "esl2"


def test_shell_dead_is_l0(monkeypatch):
    """adbd 起来了但 shell 起不来（rootfs 损坏 / exec 失败）仍判 L0。"""
    monkeypatch.setattr(
        prober_real, "_run",
        _fake_factory("connected to 172.16.60.55:5555", False, product=None),
    )
    result = prober_real.RealXosProber().probe("172.16.60.55", 5555, 5)
    assert result.level == HealthLevel.L0
    assert result.reachable_l3 is False
    assert result.extra["shell_ok"] is False


def test_connect_failure_is_l0(monkeypatch):
    monkeypatch.setattr(
        prober_real, "_run",
        _fake_factory("failed to connect to '172.16.60.57:5555': Connection refused", True),
    )
    result = prober_real.RealXosProber().probe("172.16.60.57", 5555, 5)
    assert result.level == HealthLevel.L0
    assert result.reachable_l3 is False

def _net_fake(eth_out: str, wlan_out: str, connect_out="connected to 172.16.20.212:5555"):
    def fake_run(cmd, timeout=5):
        joined = " ".join(cmd)
        if cmd[0] == "connect":
            return 0, connect_out
        if cmd[0] == "disconnect":
            return 0, ""
        if "echo __TRES_SHELL_OK__" in joined:
            return 0, "__TRES_SHELL_OK__"
        if "eth0/address" in joined:
            return 0, eth_out
        if "wlan0/address" in joined:
            return 0, wlan_out
        if "product_name" in joined:
            return 0, "esl2"
        return 0, ""

    return fake_run


def test_fe_prefix_mac_is_used_as_identity(monkeypatch):
    """fe: 前缀是 local-admin 位，实测烧录固定，应采信为指纹（v0.28.2 口径）。"""
    monkeypatch.setattr(
        prober_real, "_run",
        _net_fake(
            "cat: can't open '/sys/class/net/eth0/address': No such file or directory",
            "fe:47:b5:cd:b5:0d",
        ),
    )
    r = prober_real.RealXosProber().probe("172.16.20.212", 5555, 5)
    assert r.level == HealthLevel.L3
    assert r.mac == "fe:47:b5:cd:b5:0d"
    assert r.extra["mac_source"] == "wlan0"
    assert r.extra["mac_is_local_admin"] is True


def test_all_zero_mac_is_rejected(monkeypatch):
    monkeypatch.setattr(
        prober_real, "_run",
        _net_fake("00:00:00:00:00:00", "00:00:00:00:00:00"),
    )
    r = prober_real.RealXosProber().probe("172.16.20.212", 5555, 5)
    assert r.level == HealthLevel.L3  # shell 活仍是 L3
    assert r.mac is None
    assert r.extra["mac_source"] is None


def test_eth0_preferred_over_wlan(monkeypatch):
    monkeypatch.setattr(
        prober_real, "_run",
        _net_fake("0c:c1:19:90:0b:eb", "fe:0f:e2:1e:1c:5f"),
    )
    r = prober_real.RealXosProber().probe("172.16.60.92", 5555, 5)
    assert r.mac == "0c:c1:19:90:0b:eb"
    assert r.extra["mac_source"] == "eth0"
    assert r.extra["mac_is_local_admin"] is False
