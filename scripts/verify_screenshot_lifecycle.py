"""最小验证（v0.24）：对一台可达设备截图 -> 元数据入库 -> per-device prune
只保留最新 N；并验证 rotate 透传、单条删除、全局 prune、统计。

用临时 SQLite 库（不会污染线上 quamm_res.db）；截图文件落在
app/var/screenshots（gitignore var/），收尾统一清理。
注意：设备端 vendor 二进制 xosscreen 的 -r 90/180/270 在本机实测会
Segmentation fault（旋转是二进制自身 bug，非本系统逻辑），因此本实机验证
统一用 rotate=0；rotate 取值透传由 tests/test_screenshot_lifecycle.py 的
mocked 单测覆盖（不依赖设备）。
用法（仓库根目录）：
    timeout 60 .venv/bin/python scripts/verify_screenshot_lifecycle.py
    timeout 60 .venv/bin/python scripts/verify_screenshot_lifecycle.py --ip 172.16.10.246 --max-per-device 2 --shots 3

所有 adb 命令由 app/screenshot.py 内部 GNU timeout(15s) + subprocess 双保险包裹，
本脚本整段再用外层 timeout 包裹，绝不无限挂起。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import screenshot  # noqa: E402
from app import screenshot_lifecycle as lc  # noqa: E402
from app.db import SessionLocal, configure, init_db  # noqa: E402
from app.migrations import migrate  # noqa: E402
from app.models import Device, ScreenshotRecord  # noqa: E402
from app.runtime_config import set_runtime_value  # noqa: E402
from app.utils import utcnow  # noqa: E402

DEVICE_ID = "dev_verify_lifecycle"


def capture_with_ip(session, ip: str, port: int, quality: int, rotate: int) -> dict:
    dev = session.get(Device, DEVICE_ID)
    dev.ip = ip
    dev.adb_port = port
    session.commit()
    return screenshot.capture_screenshot(
        device_id=DEVICE_ID,
        quality=quality,
        rotate=rotate,
        session=session,
        created_by="verify_lifecycle",
    )


def count(session, device_id: str) -> int:
    return (
        session.query(ScreenshotRecord)
        .filter(ScreenshotRecord.device_id == device_id)
        .count()
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="截图生命周期最小验证")
    ap.add_argument("--ip", default="172.16.10.246", help="首选设备 IP")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--max-per-device", type=int, default=2, help="在线配置：每台最多保留 N 张")
    ap.add_argument("--shots", type=int, default=3, help="连续截图次数（须 > N 才会触发 prune）")
    ap.add_argument("--fallback-ips", default="172.16.60.16,172.16.60.198",
                    help="首选不可达时依次尝试的备用 IP（逗号分隔）")
    args = ap.parse_args()

    if args.shots <= args.max_per_device:
        print("[verify] FAIL：--shots 必须 > --max-per-device 才能验证 prune")
        return 1

    tmp = tempfile.mkdtemp(prefix="quamm_shot_verify_")
    configure("sqlite:///" + str(Path(tmp) / "verify.db"))
    init_db()
    session = SessionLocal()
    migrate(session)  # 幂等：新表 create_all 已建，这里再确认不报错
    set_runtime_value(session, "screenshot_max_per_device", args.max_per_device)
    session.add(Device(
        device_id=DEVICE_ID, owner="verify", status="available", health_level="L3",
        ip=args.ip, adb_port=args.port, created_at=utcnow(),
    ))
    session.commit()
    print(f"[verify] 临时库 {tmp}；策略 screenshot_max_per_device={args.max_per_device}", flush=True)

    candidates = [args.ip] + [x.strip() for x in args.fallback_ips.split(",") if x.strip()]
    chosen_ip = None
    results = []
    for idx in range(args.shots):
        rotate = 0  # 实机统一 rotate=0（见文件头：设备端非零旋转会 segfault）
        done = False
        for ip in candidates:
            addr = f"{ip}:{args.port}"
            if not screenshot._connect(addr, timeout=15):
                print(f"[verify] {addr} adb connect 失败，跳过", flush=True)
                continue
            try:
                r = capture_with_ip(session, ip, args.port, 60, rotate)
                chosen_ip = ip
                results.append(r)
                done = True
                break
            except Exception as e:  # noqa: BLE001 - 单台失败换下一台
                print(f"[verify] {addr} 截图失败：{type(e).__name__}: {e}", flush=True)
        if not done:
            print("[verify] FAIL：所有候选设备均不可达，无法完成截图", flush=True)
            session.close()
            return 1
        n = count(session, DEVICE_ID)
        print(f"[verify] 第 {idx + 1} 张：ip={chosen_ip} rotate={rotate} "
              f"file={r['file_name']} bytes={r['bytes']} -> 该设备记录数={n}", flush=True)
        # 每次截图后由 capture_screenshot 自动 prune_device；记录数应收敛到 min(idx+1, N)
        expect = min(idx + 1, args.max_per_device)
        if n != expect:
            print(f"[verify] FAIL：第 {idx + 1} 张后记录数 {n} != 期望 {expect}", flush=True)
            session.close()
            return 1

    final = count(session, DEVICE_ID)
    if final != args.max_per_device:
        print(f"[verify] FAIL：最终记录数 {final} != N={args.max_per_device}")
        session.close()
        return 1

    # 被 prune 删掉的最旧文件应已从磁盘消失；最新 N 张文件仍在
    pruned = results[:-args.max_per_device]
    kept = results[-args.max_per_device:]
    for r in pruned:
        if (screenshot.SCREENSHOT_DIR / r["file_name"]).exists():
            print(f"[verify] FAIL：被 prune 的文件仍存在 {r['file_name']}")
            session.close()
            return 1
    if not all((screenshot.SCREENSHOT_DIR / r["file_name"]).is_file() for r in kept):
        print("[verify] FAIL：应保留的截图文件缺失")
        session.close()
        return 1
    print(f"[verify] per-device prune OK：删除 {len(pruned)} 张最旧截图，"
          f"仅保留最新 {len(kept)} 张", flush=True)

    # rotate 取值合法性（0/90/180/270 均被接受；实机非零旋转是 vendor 二进制 bug，另由单测覆盖透传）
    for rv in (0, 90, 180, 270):
        screenshot._validate(chosen_ip, args.port, 60, rv, "/dev/fb0")
    print("[verify] rotate 取值 0/90/180/270 校验 OK（透传见 mocked 单测）", flush=True)

    # 列举 / 统计 / 单条删除 / 全局 prune
    rows = screenshot.list_screenshots(session, device_id=DEVICE_ID)
    st = lc.stats(session)
    print("[verify] list_screenshots=" + str(len(rows)) + " stats=" + json.dumps(st), flush=True)
    one = rows[0]
    shot_path = screenshot.SCREENSHOT_DIR / one["file_name"]
    if not screenshot.delete_screenshot(session, shot_id=one["shot_id"]) or shot_path.exists():
        print("[verify] FAIL：单条删除记录/文件失败")
        session.close()
        return 1
    print("[verify] delete_screenshot OK（记录 + 文件同删）", flush=True)
    print("[verify] prune_global=" + json.dumps(lc.prune_global(session, actor="verify")), flush=True)

    # 收尾：清掉临时库里该设备剩余截图文件，不留运行产物
    lc.delete_device_screenshots(session, DEVICE_ID, actor="verify", reason="verify_cleanup")
    session.close()

    print("[verify] PASS：截图成功 / 记录入库 / per-device prune 只保留最新 N / "
          "rotate / 删除 / 全局 prune 均通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
