"""v0.23 设备截图 / 芯片浮点能力：纯函数单测（不依赖真实设备）。"""
from __future__ import annotations

import pytest

from app import screenshot
from app.domain.catalog import DomainError
from app.domain import prober_real


# ---- /proc/cpuinfo Features -> 浮点 ABI 判定 ----

@pytest.mark.parametrize(
    "features, expect_hard",
    [
        ("half thumb fastmult vfp edsp neon vfpv3 vfpv4", True),   # qm10xv_full 实测
        ("half thumb vfpv3", True),                                  # vfpv3
        ("neon", True),                                              # neon
        ("half thumb fastmult edsp", False),                          # 无 FPU
        ("", False),
    ],
)
def test_features_have_fpu(features, expect_hard):
    assert screenshot._features_have_fpu(features) is expect_hard


def test_parse_features():
    cpuinfo = "processor\t: 0\nFeatures\t: half thumb vfp neon vfpv3 \nCPU part\t: 0xc07\n"
    assert screenshot._parse_features(cpuinfo) == "half thumb vfp neon vfpv3"
    assert screenshot._parse_features("no features line") is None


def test_prober_parse_cpuinfo():
    out = (
        "processor\t: 0\r\n"
        "Features\t: half thumb fastmult vfp edsp neon vfpv3 \r\n"
        "CPU implementer\t: 0x41\r\n"
        "CPU part\t: 0xc07\r\n"
    )
    features, part, impl, has_fpu = prober_real._parse_cpuinfo(out)
    assert "vfp" in features and "neon" in features
    assert part == "0xc07" and impl == "0x41" and has_fpu is True


# ---- ProductType 配置优先级 ----

@pytest.mark.parametrize(
    "cfg, expect",
    [
        ({"float_abi": "hard", "has_fpu": False}, "hard"),
        ({"float_abi": "soft", "has_fpu": True}, "soft"),
        ({"float_abi": "auto", "has_fpu": True}, "hard"),
        ({"float_abi": "auto", "has_fpu": False}, None),
        ({}, None),
        (None, None),
    ],
)
def test_config_float_abi(cfg, expect):
    assert screenshot._config_float_abi(cfg) == expect


# ---- 入参校验 / 文件名安全 ----

@pytest.mark.parametrize("quality", [0, 101, -1])
def test_validate_quality(quality):
    with pytest.raises(DomainError):
        screenshot._validate("172.16.10.246", 5555, quality, 0, "/dev/fb0")


@pytest.mark.parametrize("rotate", [1, 45, 360])
def test_validate_rotate(rotate):
    with pytest.raises(DomainError):
        screenshot._validate("172.16.10.246", 5555, 60, rotate, "/dev/fb0")


def test_validate_framebuffer_injection_blocked():
    with pytest.raises(DomainError):
        screenshot._validate("172.16.10.246", 5555, 60, 0, "/dev/fb0; rm -rf /")


@pytest.mark.parametrize(
    "name, safe",
    [
        ("dev_abc_20261109T060131018858Z.jpg", True),
        ("172.16.10.246_20261109T060131018858Z.jpg", True),
        ("../quamm_res.db", False),
        ("a/b.jpg", False),
        ("evil.png", False),
        ("..%2fetc.jpg", False),
    ],
)
def test_safe_filename_regex(name, safe):
    assert bool(screenshot._SAFE_FILENAME_RE.fullmatch(name)) is safe


def test_valid_jpeg(tmp_path):
    good = tmp_path / "good.jpg"
    good.write_bytes(b"\xff\xd8\xff" + b"\x00" * 200 + b"\xff\xd9")
    assert screenshot._valid_jpeg(good) is True
    bad = tmp_path / "bad.jpg"
    bad.write_bytes(b"not a jpeg")
    assert screenshot._valid_jpeg(bad) is False
