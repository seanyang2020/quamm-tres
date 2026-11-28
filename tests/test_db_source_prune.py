"""缓存清理返回契约：根目录不存在时也必须带 deleted_count（t1126）。"""
from __future__ import annotations

from app import cache_prune, db_source


def test_prune_db_source_missing_root_has_deleted_count(tmp_path, monkeypatch):
    """WORK_ROOT 不存在时早退也返回 deleted_count=0，且不抛异常。"""
    missing = tmp_path / "no_such_db_source"
    monkeypatch.setattr(cache_prune, "WORK_ROOT", missing)

    result = cache_prune.prune_db_source()

    assert result["deleted_count"] == 0
    assert result["deleted"] == []
    assert result["freed_bytes"] == 0


def test_prune_work_root_missing_root_no_keyerror(tmp_path, monkeypatch):
    """db_source.prune_work_root 在 WORK_ROOT 不存在时不抛 KeyError。"""
    missing = tmp_path / "no_such_db_source"
    monkeypatch.setattr(cache_prune, "WORK_ROOT", missing)
    monkeypatch.setattr(db_source, "WORK_ROOT", missing)

    result = db_source.prune_work_root()

    assert result == {"deleted_count": 0, "freed_bytes": 0}


def test_prune_db_source_empty_root_has_deleted_count(tmp_path, monkeypatch):
    """正常路径（空目录）保持原契约：deleted_count=0。"""
    empty = tmp_path / "db_source"
    empty.mkdir()
    monkeypatch.setattr(cache_prune, "WORK_ROOT", empty)

    result = cache_prune.prune_db_source()

    assert result["deleted_count"] == 0
    assert result["freed_bytes"] == 0


def test_prune_work_root_empty_root(tmp_path, monkeypatch):
    """空目录下 prune_work_root 同样返回 0。"""
    empty = tmp_path / "db_source"
    empty.mkdir()
    monkeypatch.setattr(cache_prune, "WORK_ROOT", empty)
    monkeypatch.setattr(db_source, "WORK_ROOT", empty)

    result = db_source.prune_work_root()

    assert result == {"deleted_count": 0, "freed_bytes": 0}
