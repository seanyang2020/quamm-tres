#!/usr/bin/env python3
"""排队 offer 链路真服务实测（两分支都跑通，全部走真实 MCP + REST）。

分支 1（窗口内 confirm）：
  holder 占用一台设备 → A 申请同目标进队 position=1 → B 再申请 position=2
  → holder 释放 → A 收到 offer（60s 窗口），B 仍在等待
  → A 在窗口内 res_ticket 确认 → A 拿到租约 → 清场后队列清空

分支 2（超时顺延）：
  同上排队 → holder 释放 → A 收到 offer 但不确认 → 等 offer 窗口超时
  → A 的 ticket 被取消，offer 顺延给 B → B 确认拿到租约 → 清场

前置：服务已在 :8080 运行（Web + MCP 同进程）。脚本会自动确保有 2 台可用设备，
不足时用 mock 设备补注册（走真实 res_device_probe/register）。

用法：.venv/bin/python scripts/demo_queue_offer.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp_client import API_BASE, McpClient, McpError, load_tokens, mask_token  # noqa: E402

HOLDER_EMAIL = "user01@corp.com"
A_EMAIL = "user02@corp.com"
B_EMAIL = "user03@corp.com"

_FAILURES: list[str] = []


def _check(ok: bool, msg: str) -> None:
    print(f"    {'[PASS]' if ok else '[FAIL]'} {msg}")
    if not ok:
        _FAILURES.append(msg)


def _show(step: str, msg: str) -> None:
    print(f"\n[{step}]")
    print(f"    {msg}")


def _queue_entry(entry_id: str) -> dict | None:
    q = httpx.get(f"{API_BASE}/api/queue", timeout=15).json()
    for e in q["entries"]:
        if e["entry_id"] == entry_id:
            return e
    return None


def _offer_window_seconds() -> int:
    try:
        s = httpx.get(f"{API_BASE}/api/settings", timeout=15).json()
        return int(s.get("offer_window_seconds") or 60)
    except httpx.HTTPError:
        return 60


def _claimable_devices(client: McpClient) -> list[dict]:
    devs = client.call("res_list_devices", {"claimable_only": True})
    return [d for d in devs if d.get("status") == "available"]


def _ensure_free_devices(client: McpClient, need: int) -> list[str]:
    """确保至少有 need 台可用设备，不足时用 mock 设备补注册。"""
    from tests.mock_device import MockDevice

    free = _claimable_devices(client)
    ids = [d["device_id"] for d in free]
    while len(ids) < need:
        dev = MockDevice(model="QM102V").start()
        try:
            probe = client.call("res_device_probe", {"ip": dev.ip, "port": dev.port, "hint_model": "QM102V"})
            reg = client.call(
                "res_device_register",
                {
                    "form_id": probe["form_id"],
                    "position_code": f"302-R9-{len(ids) + 1:02d}",
                    "owner": "queue-demo@corp.com",
                    "note": "queue demo",
                },
            )
            if reg.get("error"):
                raise McpError(reg.get("message", str(reg)), code=reg.get("error"))
            ids.append(reg["device_id"])
        finally:
            dev.stop()
    return ids[:need]


def _branch_confirm(tokens: dict[str, str], device_id: str) -> None:
    holder = McpClient(tokens[HOLDER_EMAIL])
    a = McpClient(tokens[A_EMAIL])
    b = McpClient(tokens[B_EMAIL])
    try:
        # 1. holder 占用
        h = holder.call("res_apply", {"target": device_id, "purpose": "queue demo holder"})
        _check(h.get("state") == "GRANTED", f"holder 占用 {device_id} -> GRANTED")
        holder_lease = h["lease"]["lease_id"]
        _show("holder", f"lease_id={holder_lease} adb={h['lease']['adb_address']}")

        # 2. A 排队 position=1
        ra = a.call("res_apply", {"target": device_id, "purpose": "queue demo A"})
        _check(ra.get("state") == "QUEUED", f"A 进队 -> {ra.get('state')}")
        _check(ra.get("position") == 1, f"A position=1（实际 {ra.get('position')}）")
        ticket_a = ra["ticket_id"]
        _show("A 进队", f"ticket={ticket_a} position={ra.get('position')}")

        # 3. B 排队 position=2
        rb = b.call("res_apply", {"target": device_id, "purpose": "queue demo B"})
        _check(rb.get("state") == "QUEUED", f"B 进队 -> {rb.get('state')}")
        _check(rb.get("position") == 2, f"B position=2（实际 {rb.get('position')}）")
        ticket_b = rb["ticket_id"]
        _show("B 进队", f"ticket={ticket_b} position={rb.get('position')}")

        # 4. holder 释放 -> A 收到 offer，B 仍在等待
        holder.call("res_release", {"lease_id": holder_lease, "reason": "queue demo release"})
        ea = _queue_entry(ticket_a)
        eb = _queue_entry(ticket_b)
        _check(ea is not None and ea["state"] == "OFFERED", f"A 状态=OFFERED（实际 {ea and ea.get('state')}）")
        _check(eb is not None and eb["state"] == "QUEUED", f"B 仍在等待 QUEUED（实际 {eb and eb.get('state')}）")
        _show(
            "holder 释放",
            f"A OFFERED（offer 剩余 {ea['offer_remaining_seconds'] if ea else '?'}s），"
            f"B QUEUED position={eb['position'] if eb else '?'}",
        )

        # 5. A 窗口内 confirm -> 拿到租约
        ca = a.call("res_ticket", {"ticket_id": ticket_a})
        _check(ca.get("state") == "GRANTED", f"A 确认 -> {ca.get('state')}")
        lease_a = ca.get("lease") or {}
        _show("A confirm", f"state=GRANTED lease_id={lease_a.get('lease_id')} adb={lease_a.get('adb_address')}")
        eb = _queue_entry(ticket_b)
        _check(eb is not None and eb["state"] == "QUEUED", f"确认后 B 仍 QUEUED（实际 {eb and eb.get('state')}）")

        # 6. 清场：A 释放 -> B offer -> B confirm -> B 释放
        a.call("res_release", {"lease_id": lease_a["lease_id"], "reason": "cleanup"})
        eb = _queue_entry(ticket_b)
        _check(eb is not None and eb["state"] == "OFFERED", f"清场：B 收到 offer（实际 {eb and eb.get('state')}）")
        cb = b.call("res_ticket", {"ticket_id": ticket_b})
        _check(cb.get("state") == "GRANTED", f"清场：B 确认 -> {cb.get('state')}")
        if cb.get("lease"):
            b.call("res_release", {"lease_id": cb["lease"]["lease_id"], "reason": "cleanup"})

        # 队列已清空
        q = httpx.get(f"{API_BASE}/api/queue", timeout=15).json()
        residual = [e for e in q["entries"] if e["target_code"] == device_id and e["state"] in ("QUEUED", "OFFERED")]
        _check(len(residual) == 0, f"队列清空（残留 {len(residual)} 条 QUEUED/OFFERED）")
    finally:
        holder.close()
        a.close()
        b.close()


def _branch_timeout(tokens: dict[str, str], device_id: str) -> None:
    holder = McpClient(tokens[HOLDER_EMAIL])
    a = McpClient(tokens[A_EMAIL])
    b = McpClient(tokens[B_EMAIL])
    try:
        h = holder.call("res_apply", {"target": device_id, "purpose": "queue timeout holder"})
        _check(h.get("state") == "GRANTED", f"holder 占用 {device_id} -> GRANTED")
        holder_lease = h["lease"]["lease_id"]

        ra = a.call("res_apply", {"target": device_id, "purpose": "queue timeout A"})
        ticket_a = ra["ticket_id"]
        _check(ra.get("position") == 1, f"A position=1（实际 {ra.get('position')}）")
        rb = b.call("res_apply", {"target": device_id, "purpose": "queue timeout B"})
        ticket_b = rb["ticket_id"]
        _check(rb.get("position") == 2, f"B position=2（实际 {rb.get('position')}）")

        holder.call("res_release", {"lease_id": holder_lease, "reason": "timeout demo release"})
        ea = _queue_entry(ticket_a)
        _check(ea is not None and ea["state"] == "OFFERED", f"A OFFERED（实际 {ea and ea.get('state')}）")

        window = _offer_window_seconds()
        _show("A 不确认", f"等待 offer 窗口超时（{window}s）…")
        # 等窗口过期后再让 A 查询，触发顺延
        time.sleep(window + 2)

        ta = a.call("res_ticket", {"ticket_id": ticket_a})
        _check(ta.get("state") == "CANCELLED", f"A 超时取消 -> {ta.get('state')}（reason={ta.get('reason')}）")

        eb = _queue_entry(ticket_b)
        _check(eb is not None and eb["state"] == "OFFERED", f"offer 顺延给 B -> OFFERED（实际 {eb and eb.get('state')}）")

        cb = b.call("res_ticket", {"ticket_id": ticket_b})
        _check(cb.get("state") == "GRANTED", f"B 确认 -> {cb.get('state')}")
        if cb.get("lease"):
            b.call("res_release", {"lease_id": cb["lease"]["lease_id"], "reason": "timeout cleanup"})

        q = httpx.get(f"{API_BASE}/api/queue", timeout=15).json()
        residual = [e for e in q["entries"] if e["target_code"] == device_id and e["state"] in ("QUEUED", "OFFERED")]
        _check(len(residual) == 0, f"队列清空（残留 {len(residual)} 条 QUEUED/OFFERED）")
    finally:
        holder.close()
        a.close()
        b.close()


def main() -> int:
    try:
        tokens = load_tokens()
    except Exception as exc:  # noqa: BLE001
        print(f"读取 token 失败：{exc}")
        return 1
    for email in (HOLDER_EMAIL, A_EMAIL, B_EMAIL):
        if email not in tokens:
            print(f"token 缓存缺少 {email}，请先运行 scripts/demo_seed_devices.py")
            return 1

    print(f"MCP: {API_BASE}/mcp/")
    print(f"身份: holder={HOLDER_EMAIL} A={A_EMAIL} B={B_EMAIL}")

    try:
        with McpClient(tokens[HOLDER_EMAIL]) as client:
            ids = _ensure_free_devices(client, need=2)
        print(f"可用设备: {ids}")
    except McpError as exc:
        print(f"准备设备失败: {exc.code} {exc}")
        return 1

    print("\n===== 分支 1：窗口内 confirm =====")
    _branch_confirm(tokens, ids[0])

    print("\n===== 分支 2：超时顺延 =====")
    _branch_timeout(tokens, ids[1])

    print()
    if _FAILURES:
        print(f"结果: FAIL（{len(_FAILURES)} 项断言未通过）")
        return 1
    print("结果: PASS（confirm 分支 + 超时顺延分支 全部通过）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
