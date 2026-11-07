"""配额上限：holder 并发上限默认 2。"""
from __future__ import annotations

from app.mcp.server import res_apply

from tests.conftest import register_device
from tests.mock_device import MockDevice


def test_quota_exceeded(db):
    devices = []
    ids = []
    for i in range(3):
        dev = MockDevice(model="QM102V").start()
        reg = register_device(db, "user01@corp.com", dev, f"302-R5-{i:02d}", "owner@corp.com")
        devices.append(dev)
        ids.append(reg["device_id"])

    try:
        r1 = db.call("user02@corp.com", res_apply, target=ids[0])
        assert r1["state"] == "GRANTED"
        r2 = db.call("user02@corp.com", res_apply, target=ids[1])
        assert r2["state"] == "GRANTED"
        r3 = db.call("user02@corp.com", res_apply, target=ids[2])
        assert r3.get("error") == "QUOTA_EXCEEDED", r3
    finally:
        for d in devices:
            d.stop()
