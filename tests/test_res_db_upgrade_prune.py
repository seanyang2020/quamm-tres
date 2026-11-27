"""res_db_upgrade 缓存清理 best-effort：清理失败不得中断真正的 DB 升级（t1126）。"""
from __future__ import annotations

from app import db_source
from app.mcp import server


class _FakeSession:
    def close(self) -> None:
        pass


def _stub_upgrade_pipeline(monkeypatch, calls: list[str]):
    """把除缓存清理外的升级链路全部打桩，避免真实网络/下载/刷机。"""
    monkeypatch.setattr(server, "require_auth", lambda: object())
    monkeypatch.setattr(server, "SessionLocal", lambda: _FakeSession())
    monkeypatch.setattr(server, "_resolve_addr", lambda *a, **k: ("10.0.0.9", 5555))

    monkeypatch.setattr(db_source, "device_db_version",
                        lambda *a, **k: {"ip": "10.0.0.9", "version": 1337})
    monkeypatch.setattr(db_source, "find_versions",
                        lambda *a, **k: (calls.append("find_versions") or
                                         {"selected": 1338, "version_url": "http://x/1338/",
                                          "strategy": "specified"}))
    monkeypatch.setattr(db_source, "list_packages",
                        lambda *a, **k: [{"name": "CURRENT.tar.gz", "url": "http://x/1338/CURRENT.tar.gz"}])
    monkeypatch.setattr(db_source, "download_package",
                        lambda *a, **k: (calls.append("download_package") or
                                         {"package": "/tmp/pkg.tgz", "name": "CURRENT.tar.gz"}))
    monkeypatch.setattr(db_source, "extract_and_find_ini",
                        lambda *a, **k: (calls.append("extract_and_find_ini") or
                                         {"extract_dir": "/tmp/ext", "script_ini": "/tmp/ext/qmimages/script.ini"}))
    monkeypatch.setattr(db_source, "upgrade",
                        lambda *a, **k: (calls.append("upgrade") or {"ok": True}))
    monkeypatch.setattr(db_source, "discard_work",
                        lambda *a, **k: {"removed": [], "freed_bytes": 0})


def test_prune_failure_does_not_block_upgrade(monkeypatch):
    calls: list[str] = []
    _stub_upgrade_pipeline(monkeypatch, calls)

    def _boom(*a, **k):
        raise KeyError("deleted_count")

    monkeypatch.setattr(db_source, "prune_work_root", _boom)

    result = server.res_db_upgrade(
        ip="10.0.0.9", project_path="XOS_SDK_Daily/10xv_lv")

    # 升级链路仍完整执行
    assert calls == ["find_versions", "download_package", "extract_and_find_ini", "upgrade"]
    assert result["upgrade"] == {"ok": True}
    assert result["target_version"] == 1338
    # 缓存清理降级为告警结果
    assert result["cache_pruned"]["deleted_count"] == 0
    assert result["cache_pruned"]["freed_bytes"] == 0
    assert "deleted_count" in result["cache_pruned"]["warning"]


def test_normal_prune_result_preserved(monkeypatch):
    calls: list[str] = []
    _stub_upgrade_pipeline(monkeypatch, calls)
    monkeypatch.setattr(db_source, "prune_work_root",
                        lambda *a, **k: {"deleted_count": 3, "freed_bytes": 1024})

    result = server.res_db_upgrade(
        ip="10.0.0.9", project_path="XOS_SDK_Daily/10xv_lv")

    assert result["cache_pruned"] == {"deleted_count": 3, "freed_bytes": 1024}
    assert calls == ["find_versions", "download_package", "extract_and_find_ini", "upgrade"]
