#!/usr/bin/env python3
"""端到端演示：user01 的 token 走一遍「查身份 → 看余量 → 选设备 → 申请 → 续约 → 释放」。

所有动作都通过 MCP streamable HTTP 调用，token 全程只显示掩码。
可重复运行：释放后设备回到可用池，再次申请同样能拿到 GRANTED。

前置：先跑 scripts/demo_seed_devices.py 注册设备。

用法：.venv/bin/python scripts/demo_lease_flow.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_client import API_BASE, McpClient, McpError, load_tokens, mask_token  # noqa: E402

EMAIL = "user01@corp.com"
MODEL = "QM102V"


def _show(step: str, request: str, response: str) -> None:
    print(f"\n[{step}]")
    print(f"  请求: {request}")
    print(f"  响应: {response}")


def main() -> int:
    try:
        tokens = load_tokens()
    except Exception as exc:  # noqa: BLE001
        print(f"读取 token 失败：{exc}")
        print("请先运行 .venv/bin/python scripts/demo_seed_devices.py")
        return 1
    if EMAIL not in tokens:
        print(f"token 缓存中没有 {EMAIL}，请先运行 scripts/demo_seed_devices.py")
        return 1

    token = tokens[EMAIL]
    print(f"MCP: {API_BASE}/mcp/    身份: {EMAIL}    token: {mask_token(token)}")

    lease_id: str | None = None
    released = False
    try:
        with McpClient(token) as client:
            who = client.call("res_whoami")
            _show(
                "1 res_whoami",
                "{}",
                f"email={who.get('email')} quota={who.get('quota')} "
                f"active_leases={who.get('active_leases')} is_admin={who.get('is_admin')}",
            )
            if who.get("email") != EMAIL:
                print("身份不符，终止")
                return 1

            products = client.call("res_list_products")
            row = next((p for p in products if p["type_code"] == MODEL), None)
            if row is None:
                print(f"机型字典中没有 {MODEL}，终止")
                return 1
            total_available = sum(p["available"] for p in products)
            _show(
                "2 res_list_products",
                "{}",
                f"{MODEL}: total={row['total']} available={row['available']} "
                f"occupied={row['occupied']}；全部机型可用合计={total_available}",
            )

            candidates = client.call("res_list_devices", {"model_class": MODEL})
            available = [d for d in candidates if d["status"] == "available"]
            _show(
                "3 res_list_devices",
                f'{{"model_class": "{MODEL}"}}',
                f"候选 {len(candidates)} 台，其中 available {len(available)} 台；"
                f"device_id={[d['device_id'] for d in available[:5]]}",
            )
            if not available:
                print("没有可用设备，请先运行 scripts/demo_seed_devices.py，终止")
                return 1
            device_id = available[0]["device_id"]

            applied = client.call(
                "res_apply",
                {"target": device_id, "purpose": "demo lease flow", "expected_minutes": 30},
            )
            if applied.get("state") != "GRANTED":
                print(f"申请未直接授予：{applied}")
                return 1
            lease = applied["lease"]
            lease_id = lease["lease_id"]
            _show(
                "4 res_apply",
                f'{{"target": "{device_id}", "expected_minutes": 30}}',
                f"state=GRANTED lease_id={lease_id} adb_address={lease['adb_address']} "
                f"expires_at={lease['expires_at']}",
            )

            mine = client.call("res_my_leases")
            _show(
                "5 res_my_leases",
                "{}",
                f"持有 {len(mine)} 条：" + ", ".join(
                    f"{l['lease_id']}@{l['device_id']}({l['state']})" for l in mine
                ),
            )
            if not any(l["lease_id"] == lease_id for l in mine):
                print("持有的租约里没有刚申请的那条，终止")
                return 1

            before = lease["expires_at"]
            renewed = client.call("res_renew", {"lease_id": lease_id, "extra_minutes": 30})
            after = renewed["expires_at"]
            _show(
                "6 res_renew",
                f'{{"lease_id": "{lease_id}", "extra_minutes": 30}}',
                f"state={renewed['state']} expires_at: {before} -> {after}",
            )
            if after <= before:
                print("续约后到期时间没有后移，终止")
                return 1

            done = client.call("res_release", {"lease_id": lease_id, "reason": "demo done"})
            released = True
            _show(
                "7 res_release",
                f'{{"lease_id": "{lease_id}", "reason": "demo done"}}',
                f"state={done['state']} ok={done['ok']}",
            )

            pool = client.call("res_list_devices", {"model_class": MODEL, "status": "available"})
            _show(
                "8 回到可用池",
                f'{{"model_class": "{MODEL}", "status": "available"}}',
                f"{MODEL} 可用 {len(pool)} 台；{device_id} 在池中="
                f"{any(d['device_id'] == device_id for d in pool)}",
            )
            if not any(d["device_id"] == device_id for d in pool):
                print("释放后设备未回到可用池")
                return 1
    except McpError as exc:
        print(f"\nMCP 调用失败: {exc.code} {exc}")
        return 1
    finally:
        # 异常中断时兜底释放，保证脚本可重复运行
        if lease_id and not released:
            try:
                with McpClient(token) as client:
                    client.call("res_release", {"lease_id": lease_id, "reason": "demo cleanup"})
                    print(f"\n已兜底释放租约 {lease_id}")
            except McpError as exc:
                print(f"\n兜底释放失败: {exc}")

    print("\n演示完成：查身份 → 看余量 → 选设备 → 申请 → 续约 → 释放 → 回池 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
