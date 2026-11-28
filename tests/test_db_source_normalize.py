"""db_source.normalize_project_spec / 特征词 / 选包规则。"""
from __future__ import annotations

from app import db_source


def test_normalize_project_spec_relative(monkeypatch):
    monkeypatch.setattr(
        "app.config.settings.xos_base_url", "http://172.16.1.201/Daily/XOS"
    )
    r = db_source.normalize_project_spec("esl2")
    assert r["normalized"] == "esl2"
    assert r["project_url"] == "http://172.16.1.201/Daily/XOS/esl2/"
    assert r["source"] == "daily"


def test_normalize_project_spec_nested_relative(monkeypatch):
    monkeypatch.setattr(
        "app.config.settings.xos_base_url", "http://172.16.1.201/Daily/XOS"
    )
    r = db_source.normalize_project_spec("XOS_SDK_Daily/10xv_lv")
    assert r["normalized"] == "XOS_SDK_Daily/10xv_lv"
    assert r["project_url"].endswith("/XOS_SDK_Daily/10xv_lv/")


def test_normalize_project_spec_http_url():
    r = db_source.normalize_project_spec("http://172.16.1.201/Daily/XOS/esl2")
    assert r["project_url"] == "http://172.16.1.201/Daily/XOS/esl2/"
    assert r["normalized"] == "http://172.16.1.201/Daily/XOS/esl2/"


def test_normalize_project_spec_lan_unc():
    r = db_source.normalize_project_spec(r"\\172.16.1.201\Daily\XOS\esl2")
    assert r["project_url"] == "http://172.16.1.201/Daily/XOS/esl2/"
    assert r["normalized"].startswith("http://")


def test_normalize_project_spec_lan_ipv4_path():
    r = db_source.normalize_project_spec("172.16.1.201/Daily/XOS/moto_ld_102d")
    assert r["project_url"] == "http://172.16.1.201/Daily/XOS/moto_ld_102d/"


def test_normalize_project_spec_smb():
    r = db_source.normalize_project_spec("smb://172.16.1.201/Daily/XOS/esl2")
    assert r["project_url"] == "http://172.16.1.201/Daily/XOS/esl2/"


def test_normalize_project_spec_alias():
    r = db_source.normalize_project_spec("daily")
    assert r["source"] == "daily"
    assert r["project_url"] is None
    assert r["base_url"].endswith("/Daily/XOS/")


def test_list_version_features_strips_dates_and_digits(monkeypatch):
    html = """
    <a href="../">../</a>
    <a href="236/">236/</a>
    <a href="236_4M_release/">236_4M_release/</a>
    <a href="240_4M_debug_20241231/">240_4M_debug_20241231/</a>
    <a href="241_2024-12-01_soft/">241_2024-12-01_soft/</a>
    """
    monkeypatch.setattr(db_source, "_http", lambda url, timeout=8: html)
    info = db_source.list_version_features("http://example/proj/")
    names = {f["name"].lower() for f in info["features"]}
    assert "4m" in names
    assert "release" in names
    assert "debug" in names
    assert "soft" in names
    assert "236" not in names
    assert "20241231" not in names
    assert "2024-12-01" not in names
    assert "236_4M_release" in info["versions"]


def test_matches_features_and():
    assert db_source.matches_features("236_4M_release", ["4M", "release"])
    assert not db_source.matches_features("236_4M_debug", ["4M", "release"])
    assert db_source.matches_features("anything", [])


def test_select_best_package_prefers_debug():
    pkgs = [
        {"name": "CURRENT_esl2_100_release.tar.gz", "url": "u1", "size": 999},
        {"name": "CURRENT_esl2_100_debug.tar.gz", "url": "u2", "size": 10},
    ]
    best = db_source.select_best_package(pkgs)
    assert best["name"].endswith("_debug.tar.gz")


def test_select_best_package_largest_without_debug():
    pkgs = [
        {"name": "CURRENT_a.tar.gz", "url": "u1", "size": 100},
        {"name": "CURRENT_b.tar.gz", "url": "u2", "size": 500},
    ]
    best = db_source.select_best_package(pkgs)
    assert best["name"] == "CURRENT_b.tar.gz"


def test_parse_feature_filter_json_and_csv():
    assert db_source.parse_feature_filter('["4M","release"]') == ["4M", "release"]
    assert db_source.parse_feature_filter("4M, release") == ["4M", "release"]
    assert db_source.parse_feature_filter(None) == []
