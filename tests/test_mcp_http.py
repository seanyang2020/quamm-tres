"""MCP streamable HTTP 真实接口：初始化 + tools/call 全链路。"""
from __future__ import annotations

import json

from starlette.testclient import TestClient

from app.main import app

from tests.conftest import register_device
from tests.mock_device import MockDevice


def _mcp_post(client, token, payload):
    r = client.post(
        "/mcp/",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
        json=payload,
    )
    assert r.status_code == 200, r.text
    data_line = next(l for l in r.text.splitlines() if l.startswith("data: "))
    return json.loads(data_line[len("data: "):])


def _call_tool(client, token, name, args=None):
    resp = _mcp_post(
        client,
        token,
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args or {}}},
    )
    assert "error" not in resp, resp
    # FastMCP 把结构化结果放在 content[0].text（JSON 字符串），structuredContent 对 list 返回不可靠
    return json.loads(resp["result"]["content"][0]["text"])


def test_mcp_http_full_flow(db):
    # 预注册一台设备（setup，非被测流程）
    dev = MockDevice(model="QM102V").start()
    try:
        reg = register_device(db, "user01@corp.com", dev, "302-R7-01", "owner@corp.com")
        device_id = reg["device_id"]
        token = db.token("user02@corp.com")

        with TestClient(app) as client:
            # initialize
            init = _mcp_post(
                client, token,
                {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
            )
            assert init["result"]["serverInfo"]["name"] == "quamm-res"

            # 查机型 -> 查设备 -> 申请 -> 拿到 adb 地址 -> 续约 -> 释放
            products = _call_tool(client, token, "res_list_products")
            assert any(p["type_code"] == "QM102V" for p in products)

            devices = _call_tool(client, token, "res_list_devices")
            assert any(d["device_id"] == device_id for d in devices)

            apply_r = _call_tool(client, token, "res_apply", {"target": device_id, "purpose": "e2e"})
            assert apply_r["state"] == "GRANTED", apply_r
            lease_id = apply_r["lease"]["lease_id"]
            assert apply_r["lease"]["adb_address"] == dev.adb_address

            renew_r = _call_tool(client, token, "res_renew", {"lease_id": lease_id, "extra_minutes": 30})
            assert renew_r["state"] == "GRANTED"

            release_r = _call_tool(client, token, "res_release", {"lease_id": lease_id})
            assert release_r["ok"] is True
    finally:
        dev.stop()


def test_mcp_http_rejects_missing_token(db):
    with TestClient(app) as client:
        r = client.post(
            "/mcp/",
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        )
        assert r.status_code == 401


def test_mcp_http_res_admin_forbidden_for_normal(db):
    token = db.token("user02@corp.com")  # 非管理员
    with TestClient(app) as client:
        r = _call_tool(client, token, "res_admin", {"action": "overview"})
        assert r.get("error") == "FORBIDDEN"
