"""设备配置体检：重复登记判据只认 ADB 地址 / MAC，同镜像不算重复。"""
from __future__ import annotations

import json
from datetime import datetime

from app.domain import consistency
from app.models import Device


def _mk(session, device_id, ip, mac, sha="8751a8c2deadbeef"):
    d = Device(
        device_id=device_id,
        display_name=device_id,
        model_class=None,
        owner="owner@corp.com",
        status="available",
        health_level="L3",
        ip=ip,
        adb_port=5555,
        mac=mac,
        source="real",
        created_at=datetime.now(),
        eng_config=json.dumps({"build_info": {"config_sha256": sha}}),
    )
    session.add(d)
    return d


def test_same_firmware_sha_is_not_duplicate(db):
    s = db.session()
    a = _mk(s, "dev_a", "172.16.60.198", "a0:ac:78:a0:d7:50")
    b = _mk(s, "dev_b", "172.16.60.49", "a0:ac:78:a0:d7:51")
    s.commit()
    ca = consistency.check_device(s, a)
    assert ca["verdict"] != "duplicate"
    assert "重复登记" not in "；".join(ca["issues"])
    s.close()


def test_same_adb_address_is_duplicate(db):
    s = db.session()
    a = _mk(s, "dev_a", "172.16.60.198", "a0:ac:78:a0:d7:50")
    d = _mk(s, "dev_d", "172.16.60.198", "a0:ac:78:a0:d7:52")
    s.commit()
    ca = consistency.check_device(s, a)
    assert ca["verdict"] == "duplicate"
    assert "dev_d" in ca["issues"][0]
    s.close()


def test_audit_pool_dup_groups_ignore_sha(db):
    s = db.session()
    _mk(s, "dev_a", "172.16.60.198", "a0:ac:78:a0:d7:50")
    _mk(s, "dev_b", "172.16.60.49", "a0:ac:78:a0:d7:51")
    s.commit()
    out = consistency.audit_pool(s)
    assert out["duplicate_groups"] == []
    s.close()
