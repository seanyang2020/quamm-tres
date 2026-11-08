#!/usr/bin/env python3
"""Quamm RES 一键验收脚本（不依赖 pytest，直接打真服务）。

前置：服务已在 :8080 运行（Web + MCP 同进程，默认 sqlite:///./quamm_res.db）。
脚本自动确保证据齐全（token、至少 2 台可用设备、至少 1 条巡检 run）。

输出一张表：| # | 检查项 | 期望 | 实际 | PASS/FAIL |。
期望版本号读根目录 VERSION 文件（单一真源），不硬编码。
全部 PASS 才视为就绪，退出码 0；任一 FAIL 退出码非 0。

用法：
  .venv/bin/python scripts/verify_v1.py
  RES_DEMO_API_BASE=http://127.0.0.1:8080 RES_VERIFY_DB_URL=sqlite:///./quamm_res.db .venv/bin/python scripts/verify_v1.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from mcp_client import (  # noqa: E402
    API_BASE,
    McpClient,
    McpError,
    load_admin_token,
    load_tokens,
)

DB_URL = os.environ.get("RES_VERIFY_DB_URL", "sqlite:///./quamm_res.db")

RESULTS: list[dict] = []


def _expected_version() -> str:
    """读取根目录 VERSION 文件（唯一真源），缺失即抛错让验收失败。"""
    return (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def _short(v, limit: int = 60) -> str:
    s = str(v)
    return s if len(s) <= limit else s[: limit - 3] + "..."


def check(name: str, expected: str, actual, ok: bool) -> None:
    RESULTS.append(
        {
            "name": name,
            "expected": expected,
            "actual": _short(actual),
            "ok": bool(ok),
        }
    )


def _get(path: str, **kw) -> httpx.Response:
    return httpx.get(f"{API_BASE}{path}", timeout=15, **kw)


def _free_device_ids(client: McpClient) -> list[str]:
    devs = client.call("res_list_devices", {"claimable_only": True})
    return [d["device_id"] for d in devs if d.get("status") == "available"]


def _ensure_devices(client: McpClient, need: int) -> list[str]:
    """确保 need 台可用设备；不足时用 mock 设备补注册（走真实 MCP 注册链路）。"""
    from tests.mock_device import MockDevice

    ids = _free_device_ids(client)
    while len(ids) < need:
        dev = MockDevice(model="QM102V").start()
        try:
            probe = client.call(
                "res_device_probe", {"ip": dev.ip, "port": dev.port, "hint_model": "QM102V"}
            )
            reg = client.call(
                "res_device_register",
                {
                    "form_id": probe["form_id"],
                    "position_code": f"302-RV-{len(ids) + 1:02d}",
                    "owner": "verify@corp.com",
                    "note": "verify v1",
                },
            )
            if reg.get("error"):
                raise McpError(reg.get("message", str(reg)), code=reg.get("error"))
            ids.append(reg["device_id"])
        finally:
            dev.stop()
    return ids[:need]


def _ensure_patrol_run() -> int:
    """确保至少 1 条巡检 run 存在；空则直接触发一轮（巡检无对外触发接口）。"""
    try:
        runs = _get("/api/patrol/runs").json()
        if isinstance(runs, list) and runs:
            return len(runs)
    except (httpx.HTTPError, ValueError):
        pass

    from app.db import SessionLocal, configure, init_db
    from app.domain import patrol
    from app.domain.probe import MockProber, set_prober

    configure(DB_URL)
    init_db()
    set_prober(MockProber())
    session = SessionLocal()
    try:
        patrol.run_patrol_round(session)
    finally:
        session.close()

    try:
        runs = _get("/api/patrol/runs").json()
        return len(runs) if isinstance(runs, list) else 0
    except (httpx.HTTPError, ValueError):
        return 0


def _run_admin_rejection(client: McpClient, email: str) -> None:
    try:
        r = client.call("res_admin", {"action": "overview"})
        rejected = isinstance(r, dict) and r.get("error") == "FORBIDDEN"
    except McpError as exc:
        rejected = exc.code == "FORBIDDEN"
        r = f"FORBIDDEN ({exc})"
    check("普通 token 调 res_admin 被拒", "FORBIDDEN", r, rejected)
    # 不留管理员操作记录：审计里不能出现该普通账号的任何管理员动作
    try:
        audit = _get("/api/audit").json()
        events = audit.get("events", audit) if isinstance(audit, dict) else audit
        admin_actions = {"force_release", "forced", "stats", "overview", "list_credentials"}
        leaked = [
            e for e in events
            if e.get("actor") == email and e.get("action") in admin_actions
        ]
        check("被拒后不留管理员操作记录", "0 条", f"{len(leaked)} 条", len(leaked) == 0)
    except (httpx.HTTPError, ValueError):
        check("被拒后不留管理员操作记录", "0 条", "audit 查询失败", False)


def _run_queue_offer(client_holder: McpClient, client_a: McpClient, client_b: McpClient, device_id: str) -> None:
    h = client_holder.call("res_apply", {"target": device_id, "purpose": "verify holder"})
    check("排队前置：holder 占用设备", "GRANTED", h.get("state"), h.get("state") == "GRANTED")
    holder_lease = h["lease"]["lease_id"]

    ra = client_a.call("res_apply", {"target": device_id, "purpose": "verify A"})
    ticket_a = ra["ticket_id"]
    check("A 申请同目标进队", "QUEUED position=1", ra.get("state"), ra.get("state") == "QUEUED" and ra.get("position") == 1)

    rb = client_b.call("res_apply", {"target": device_id, "purpose": "verify B"})
    ticket_b = rb["ticket_id"]
    check("B 再申请同目标", "QUEUED position=2", rb.get("state"), rb.get("state") == "QUEUED" and rb.get("position") == 2)

    ta = client_a.call("res_ticket", {"ticket_id": ticket_a})
    check("ticket 查询", "QUEUED position=1", f"{ta.get('state')} pos={ta.get('position')}",
          ta.get("state") == "QUEUED" and ta.get("position") == 1)

    client_holder.call("res_release", {"lease_id": holder_lease, "reason": "verify release"})
    q = _get("/api/queue").json()
    ea = next((e for e in q["entries"] if e["entry_id"] == ticket_a), None)
    eb = next((e for e in q["entries"] if e["entry_id"] == ticket_b), None)
    check("占有者释放 → A 收到 offer", "OFFERED", ea and ea.get("state"),
          ea is not None and ea.get("state") == "OFFERED")
    check("B 仍在等待", "QUEUED position=1", f"{eb and eb.get('state')} pos={eb and eb.get('position')}",
          eb is not None and eb.get("state") == "QUEUED" and eb.get("position") == 1)

    ca = client_a.call("res_ticket", {"ticket_id": ticket_a})
    check("A 窗口内确认 → 拿到租约", "GRANTED", ca.get("state"),
          ca.get("state") == "GRANTED" and ca.get("lease") is not None)
    lease_a = ca["lease"]["lease_id"]

    # 清场：A 释放 → B offer → B 确认 → B 释放
    client_a.call("res_release", {"lease_id": lease_a, "reason": "verify cleanup"})
    cb = client_b.call("res_ticket", {"ticket_id": ticket_b})
    if cb.get("state") == "GRANTED" and cb.get("lease"):
        client_b.call("res_release", {"lease_id": cb["lease"]["lease_id"], "reason": "verify cleanup"})
    q2 = _get("/api/queue").json()
    residual = [e for e in q2["entries"] if e["target_code"] == device_id and e["state"] in ("QUEUED", "OFFERED")]
    check("清场后队列清空", "0 条 QUEUED/OFFERED", f"{len(residual)} 条", len(residual) == 0)


def main() -> int:
    # 服务可达性
    try:
        _get("/api/health").raise_for_status()
    except httpx.HTTPError as exc:
        print(f"服务不可达：{API_BASE} （{exc}）")
        print("请先启动：.venv/bin/python -m uvicorn app.main:app --port 8080")
        return 2

    try:
        tokens = load_tokens()
        admin_token = load_admin_token()
    except Exception as exc:  # noqa: BLE001
        print(f"token 准备失败：{exc}")
        return 2

    normal = McpClient(tokens["user01@corp.com"])
    admin = McpClient(admin_token)
    try:
        # ---- 版本 / 身份 / 健康 ----
        expected_version = _expected_version()
        v = _get("/api/version").json()
        check("版本号", f'{{"version":"{expected_version}","name":"Quamm RES","stage":"pre-release"}}', v,
              v.get("version") == expected_version and v.get("name") == "Quamm RES" and v.get("stage") == "pre-release")

        who = normal.call("res_whoami")
        check("whoami（普通）", "is_admin=false", f"email={who.get('email')} is_admin={who.get('is_admin')}",
              who.get("is_admin") is False and who.get("email") == "user01@corp.com")

        who_admin = admin.call("res_whoami")
        check("whoami（管理员）", "is_admin=true", f"email={who_admin.get('email')} is_admin={who_admin.get('is_admin')}",
              who_admin.get("is_admin") is True and who_admin.get("email") == "admin@corp.com")

        h = admin.call("res_health")
        check("res_health", "status=ok + version", f"status={h.get('status')} version={h.get('version')}",
              h.get("status") == "ok" and h.get("version") == expected_version)

        # ---- 目录 / 设备 ----
        products = normal.call("res_list_products")
        check("list_products", ">=5 类机型", f"{len(products)} 类", isinstance(products, list) and len(products) >= 5)

        devices = normal.call("res_list_devices", {"claimable_only": True})
        check("list_devices", "返回列表", f"{len(devices) if isinstance(devices, list) else devices} 台", isinstance(devices, list))

        ids = _ensure_devices(normal, need=2)
        check("可用设备准备", ">=2 台", f"{len(ids)} 台", len(ids) >= 2)
        d1, d2 = ids[0], ids[1]

        info = normal.call("res_device_info", {"device_id": d1})
        check("device_info", "返回 device_id", info.get("device_id"), info.get("device_id") == d1)

        # ---- 申请 / 续约 / 释放 ----
        apply_user = McpClient(tokens["user05@corp.com"])
        ap = apply_user.call("res_apply", {"target": d1, "purpose": "verify apply"})
        check("apply → GRANTED", "GRANTED + adb_address", ap.get("state"),
              ap.get("state") == "GRANTED" and ap.get("lease", {}).get("adb_address"))
        lease_id = ap["lease"]["lease_id"]

        mine = apply_user.call("res_my_leases")
        check("my_leases 含新租约", f"含 {lease_id}", f"{len(mine)} 条",
              isinstance(mine, list) and any(l.get("lease_id") == lease_id for l in mine))

        before = ap["lease"]["expires_at"]
        rn = apply_user.call("res_renew", {"lease_id": lease_id, "extra_minutes": 30})
        check("renew 续约", "GRANTED 且到期后移", f"{rn.get('state')} {before}->{rn.get('expires_at')}",
              rn.get("state") == "GRANTED" and rn.get("expires_at", "") > before)

        rel = apply_user.call("res_release", {"lease_id": lease_id, "reason": "verify release"})
        check("release 释放", "ok=true", rel.get("ok"), rel.get("ok") is True)
        apply_user.close()

        # ---- 管理员 ----
        _run_admin_rejection(normal, "user01@corp.com")

        st = admin.call("res_admin", {"action": "stats"})
        ok_stats = (
            isinstance(st, dict)
            and "devices" in st and "leases" in st and "queue" in st and "notices" in st
        )
        check("admin stats", "devices/leases/queue/notices 四组", _short(st), ok_stats)

        # ---- 排队 offer 流程 ----
        _run_queue_offer(
            McpClient(tokens["user10@corp.com"]),
            McpClient(tokens["user11@corp.com"]),
            McpClient(tokens["user12@corp.com"]),
            d2,
        )

        # ---- 巡检 run 存在 ----
        runs = _ensure_patrol_run()
        check("巡检 run 存在", ">=1 条", f"{runs} 条", runs >= 1)

        # ---- 8 个 REST 页面接口 ----
        pages = [
            ("/api/dashboard", "看板"),
            ("/api/devices", "设备"),
            ("/api/leases", "占用"),
            ("/api/queue", "排队"),
            ("/api/anomalies", "异常/待修复"),
            ("/api/credentials", "凭据"),
            ("/api/audit", "审计"),
            ("/api/settings", "设置"),
        ]
        for path, label in pages:
            try:
                r = _get(path)
                check(f"Web 页面接口 {label} ({path})", "200", r.status_code, r.status_code == 200)
            except httpx.HTTPError as exc:
                check(f"Web 页面接口 {label} ({path})", "200", f"ERR {exc}", False)
    except McpError as exc:
        check("脚本执行", "无异常", f"MCP {exc.code}: {exc}", False)
    except Exception as exc:  # noqa: BLE001
        check("脚本执行", "无异常", str(exc), False)
    finally:
        normal.close()
        admin.close()

    # ---- 打印表 ----
    print()
    print(f"{'#':>3} | {'检查项':<34} | {'期望':<32} | {'实际':<36} | 结果")
    print("-" * 130)
    for i, r in enumerate(RESULTS, start=1):
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"{i:>3} | {r['name']:<34} | {r['expected']:<32} | {r['actual']:<36} | {mark}")
    print("-" * 130)
    passed = sum(1 for r in RESULTS if r["ok"])
    failed = sum(1 for r in RESULTS if not r["ok"])
    print(f"合计：{len(RESULTS)} 项，PASS {passed}，FAIL {failed}")
    print()
    if failed == 0:
        try:
            ver = _expected_version()
        except OSError:
            ver = "?"
        print(f"结论：{ver} 就绪（全部 PASS）")
        return 0
    print(f"结论：未就绪（{failed} 项 FAIL）")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
