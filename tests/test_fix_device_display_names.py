"""scripts/fix_device_display_names.py 最小回归：--map 改名时自动扫描应使用新名称。"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from app.db import configure, get_session_factory, init_db
from app.models import Device, ProductType


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path}/fix.db"
    configure(url)
    init_db()
    s = get_session_factory()()
    try:
        s.add(ProductType(
            type_code="esl2_skw", display_name="esl2_skw", has_screen=True,
            has_led=False, serial_count=1, adb_port=5555, enabled=True,
            created_at=datetime.now(timezone.utc),
        ))
        s.add(Device(
            device_id="dev001", display_name="esl2_skw-302",
            model_class="esl2_skw", position_code="302", owner="技术部",
            adb_port=5555,
            created_at=datetime.now(timezone.utc),
        ))
        s.add(Device(
            device_id="dev002", display_name="自定义名",
            model_class="esl2_skw", position_code="303", owner="技术部",
            adb_port=5555,
            created_at=datetime.now(timezone.utc),
        ))
        s.commit()
    finally:
        s.close()
    return url


def _run(monkeypatch, url, *args, capsys):
    from scripts.fix_device_display_names import main
    monkeypatch.setattr(sys, "argv", ["fix_device_display_names.py", "--db", url, *args])
    return main(), capsys.readouterr().out


def test_map_makes_auto_scan_use_new_name_dry_run(db_url, monkeypatch, capsys):
    # dry-run：--map 给新名称后，技术代号设备应被列为候选改名为新名称
    code, out = _run(monkeypatch, db_url, "--map", "esl2_skw=彩屏价签_V2", capsys=capsys)
    assert code == 0
    assert "[device_auto] dev001: 'esl2_skw-302' -> '彩屏价签_V2'" in out
    assert "dry-run" in out
    # 自定义名设备不碰
    assert "dev002" not in out


def test_apply_updates_type_then_devices(db_url, monkeypatch, capsys):
    code, out = _run(
        monkeypatch, db_url, "--map", "esl2_skw=彩屏价签_V2", "--apply", capsys=capsys,
    )
    assert code == 0
    assert "已写入" in out
    s = get_session_factory()()
    try:
        pt = s.get(ProductType, "esl2_skw")
        assert pt.display_name == "彩屏价签_V2"
        # 技术代号设备随类型新名称改名
        assert s.get(Device, "dev001").display_name == "彩屏价签_V2"
        # 自定义名不被覆盖
        assert s.get(Device, "dev002").display_name == "自定义名"
    finally:
        s.close()


def test_without_map_uses_db_display_name(db_url, monkeypatch, capsys):
    # 未给 --map 且库内 display_name == type_code → 无候选，跳过
    code, out = _run(monkeypatch, db_url, capsys=capsys)
    assert code == 0
    assert "没有需要修改的条目" in out
