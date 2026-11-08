#!/usr/bin/env python3
"""用 20 个 token 走真实 MCP 链路，把 20 台模拟设备注册进主库。

流程（对每台设备，严格只用 HTTP 工具，不写库、不碰内部函数）：
  1) 本机 127.0.0.1:7001-7020 各起一个 mock 设备端点（tests/mock_device.py）
  2) 用 userNN@corp.com 的 token 调 res_device_probe
  3) 紧接着调 res_device_register（position_code 302-R2-01..20）
  4) 打印注册结果表，并从 /api/dashboard、/api/devices 复核

幂等：mock 设备的 MAC/SN/asset_tag 按序号固定，重复运行会命中已有设备
（matched_as=existing），不会新增记录；位置编号属于该设备自身时不算冲突。

用法：
  .venv/bin/python scripts/demo_seed_devices.py            # 使用/生成 token 缓存
  .venv/bin/python scripts/demo_seed_devices.py --rotate   # 强制吊销并重签 20 条 token
"""
from __future__ import annotations

import argparse
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx  # noqa: E402

from mcp_client import (  # noqa: E402
    API_BASE,
    McpClient,
    McpError,
    load_tokens,
    refresh_tokens,
)
from tests.mock_device import MockDevice  # noqa: E402

MODELS = ["QM102V", "QM10XD", "QM108H", "QM102V-Lite", "BES2800BP"]
COUNT = 20
BASE_PORT = 7001
OWNER = "lab-owner@corp.com"


def _mac_for(i: int) -> str:
    return f"02:00:00:00:{(i >> 8) & 0xFF:02X}:{i & 0xFF:02X}"


def _sn_for(i: int) -> str:
    return f"DEMO-SN-{i:04d}"


def _asset_for(i: int) -> str:
    return f"DEMO-ASSET-{i:04d}"


def _dwidth(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _dwidth(text))


def _print_table(rows: list[dict]) -> None:
    headers = ["device_id", "机型", "位置编号", "MAC", "注册人邮箱", "状态"]
    keys = ["device_id", "model", "position", "mac", "email", "status"]
    widths = [max(_dwidth(h), *( _dwidth(str(r[k])) for r in rows)) for h, k in zip(headers, keys)]
    print("  ".join(_pad(h, w) for h, w in zip(headers, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join(_pad(str(r[k]), w) for k, w in zip(keys, widths)))


def _start_devices() -> list[MockDevice]:
    devices: list[MockDevice] = []
    for i in range(COUNT):
        dev = MockDevice(
            model=MODELS[i % len(MODELS)],
            mac=_mac_for(i),
            sn=_sn_for(i),
            asset_tag=_asset_for(i),
            android_version=f"1{3 + i % 3}",
            build_fingerprint=f"{MODELS[i % len(MODELS)]}/demo/{_sn_for(i)}",
            port=BASE_PORT + i,
        )
        devices.append(dev.start())
    return devices


def _ensure_tokens(rotate: bool) -> dict[str, str]:
    if rotate:
        return refresh_tokens(COUNT)
    tokens = load_tokens(COUNT)
    # 缓存可能已失效（被 seed --rotate 吊销）：用 user01 试一次，失败则重签
    try:
        with McpClient(tokens["user01@corp.com"]) as client:
            client.call("res_whoami")
    except McpError as exc:
        if exc.code != "AUTH":
            raise
        print("token 缓存已失效，重新签发 20 条凭据")
        tokens = refresh_tokens(COUNT)
    return tokens


def main() -> int:
    parser = argparse.ArgumentParser(description="注册 20 台 mock 设备到 Quamm TRES")
    parser.add_argument("--rotate", action="store_true", help="强制吊销旧凭据并重签 20 条 token")
    args = parser.parse_args()

    try:
        tokens = _ensure_tokens(args.rotate)
    except Exception as exc:  # noqa: BLE001
        print(f"token 准备失败：{exc}")
        return 1

    print(f"服务端 MCP: {API_BASE}/mcp/   设备端点: 127.0.0.1:{BASE_PORT}-{BASE_PORT + COUNT - 1}")
    devices: list[MockDevice] = []
    rows: list[dict] = []
    errors: list[str] = []
    try:
        devices = _start_devices()
        for i, dev in enumerate(devices, start=1):
            email = f"user{i:02d}@corp.com"
            position = f"302-R2-{i:02d}"
            try:
                with McpClient(tokens[email]) as client:
                    probe = client.call(
                        "res_device_probe",
                        {
                            "ip": dev.ip,
                            "port": dev.port,
                            "hint_model": dev.model,
                            "hint_position": position,
                        },
                    )
                    reg = client.call(
                        "res_device_register",
                        {
                            "form_id": probe["form_id"],
                            "position_code": position,
                            "owner": OWNER,
                            "note": f"{dev.model} @ {position}",
                        },
                    )
            except McpError as exc:
                errors.append(f"{position} {email}: {exc.code} {exc}")
                print(f"[{i:02d}] {position} 注册失败: {exc.code} {exc}")
                continue

            rows.append(
                {
                    "device_id": reg["device_id"],
                    "model": probe["probe"].get("model") or "-",
                    "position": position,
                    "mac": probe["probe"].get("mac") or "-",
                    "email": email,
                    "status": reg["status"],
                    "matched_as": reg.get("matched_as"),
                }
            )
            print(
                f"[{i:02d}] {position} -> {reg['device_id']}  "
                f"{reg['status']}（{reg.get('matched_as')}）"
            )
    finally:
        for dev in devices:
            dev.stop()

    print()
    print("=== 注册结果 ===")
    _print_table(rows)

    new_count = sum(1 for r in rows if r["matched_as"] == "new")
    existing_count = sum(1 for r in rows if r["matched_as"] == "existing")
    print()
    print(f"本次新建 {new_count} 台，命中已有 {existing_count} 台，失败 {len(errors)} 台")

    # 从 Web API 复核（与 MCP 共用同一主库）
    try:
        dash = httpx.get(f"{API_BASE}/api/dashboard", timeout=15).json()
        devs = httpx.get(f"{API_BASE}/api/devices", params={"limit": 500}, timeout=15).json()
    except httpx.HTTPError as exc:
        print(f"复核失败（服务不可达）：{exc}")
        return 1

    positions = [d["position_code"] for d in devs]
    dup_positions = sorted({p for p in positions if positions.count(p) > 1})
    print()
    print("=== 服务端复核 ===")
    print(f"/api/dashboard total_devices = {dash['total_devices']}")
    print(f"/api/dashboard by_status     = {dash['by_status']}")
    print(f"/api/devices 返回条数         = {len(devs)}")
    print(f"位置编号重复                  = {dup_positions or '无'}")

    ok = (
        not errors
        and dash["total_devices"] == COUNT
        and len(devs) == COUNT
        and not dup_positions
    )
    print()
    print("结果:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
