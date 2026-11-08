#!/usr/bin/env python3
"""可复现的巡检闭环演示：掉线 → 摘出可用池 → 判离线并发通知 → 恢复回池。

默认在临时 SQLite 上跑，不污染主库；想对着正在运行的服务验证
``/api/patrol/runs``、``/api/notices`` 时，用 ``--db-url`` 指向服务同一个库
（此时脚本仍直接调用领域层，因为巡检没有对外触发接口）。

四个阶段（全部在线 20 台）：
  1. 起 20 台 mock → 巡检一轮 → l3=20，dashboard available=20
  2. 关掉 5 台 → 巡检一轮 → 这 5 台 suspect，available=15
  3. 仍关着 → 再巡检一轮 → 这 5 台 offline，产生 device_offline notice
  4. 重新起这 5 台 → 巡检一轮 → 全部恢复 L3，available=20

用法：
  .venv/bin/python scripts/demo_patrol_cycle.py
  .venv/bin/python scripts/demo_patrol_cycle.py --db-url sqlite:///./quamm_res.db
"""
from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import func  # noqa: E402

from app.api.router import dashboard  # noqa: E402
from app.config import settings  # noqa: E402
from app.constants import DeviceStatus, HealthLevel  # noqa: E402
from app.db import configure, get_session_factory, init_db  # noqa: E402
from app.domain import catalog, patrol  # noqa: E402
from app.domain.probe import MockProber, set_prober  # noqa: E402
from app.models import Device, Notice, PatrolRun  # noqa: E402
from app.seed import seed_product_types  # noqa: E402
from tests.mock_device import MockDevice  # noqa: E402

COUNT = 20
OFFLINE_COUNT = 5
BASE_PORT = 7101
MODELS = ["QM102V", "QM10XD", "QM108H", "QM102V-Lite", "BES2800BP"]
OWNER = "patrol-owner@corp.com"

_FAILURES: list[str] = []


def _check(ok: bool, message: str) -> None:
    if ok:
        print(f"  [PASS] {message}")
    else:
        print(f"  [FAIL] {message}")
        _FAILURES.append(message)


def _mac_for(i: int) -> str:
    return f"02:11:00:00:{(i >> 8) & 0xFF:02X}:{i & 0xFF:02X}"


def _sn_for(i: int) -> str:
    return f"PATROL-SN-{i:04d}"


def _asset_for(i: int) -> str:
    return f"PATROL-ASSET-{i:04d}"


def _start_devices() -> list[MockDevice]:
    devices = []
    for i in range(COUNT):
        dev = MockDevice(
            model=MODELS[i % len(MODELS)],
            mac=_mac_for(i),
            sn=_sn_for(i),
            asset_tag=_asset_for(i),
            port=BASE_PORT + i,
        )
        devices.append(dev.start())
    return devices


def _session():
    return get_session_factory()()


def _run_patrol() -> PatrolRun:
    session = _session()
    try:
        return patrol.run_patrol_round(session)
    finally:
        session.close()


def _register_all(devices: list[MockDevice]) -> list[str]:
    ids: list[str] = []
    session = _session()
    try:
        for i, dev in enumerate(devices, start=1):
            form = catalog.device_probe(session, dev.ip, dev.port)
            reg = catalog.device_register(
                session,
                form["form_id"],
                position_code=f"302-R9-{i:02d}",
                owner=OWNER,
                actor_email=f"user{i:02d}@corp.com",
                note=f"{dev.model} mock",
            )
            if reg.get("error") or reg["status"] != DeviceStatus.AVAILABLE:
                raise RuntimeError(f"注册失败: {reg}")
            ids.append(reg["device_id"])
    finally:
        session.close()
    return ids


def _snapshot(phase: str) -> tuple[PatrolRun, dict]:
    session = _session()
    try:
        run = session.query(PatrolRun).order_by(PatrolRun.ended_at.desc()).first()
        dash = dashboard(session=session)
    finally:
        session.close()
    print(
        f"\n[{phase}] run={run.run_id}  "
        f"scanned={run.scanned_count} l3={run.l3_count} "
        f"suspect={run.suspect_count} offline={run.offline_count}"
    )
    print(
        f"          dashboard: available={dash['available']} suspect={dash['suspect']} "
        f"by_status={dash['by_status']}"
    )
    return run, dash


def _device_rows(ids: list[str]) -> list[tuple[str, str, int]]:
    session = _session()
    try:
        return [
            (d.device_id, d.status, d.health_level)
            for d in session.query(Device).filter(Device.device_id.in_(ids)).order_by(Device.device_id)
        ]
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="巡检闭环演示（20 台 mock 设备）")
    parser.add_argument(
        "--db-url",
        default=None,
        help="默认使用临时 SQLite；指向服务同库可核对 /api/patrol/runs 与 /api/notices",
    )
    parser.add_argument("--keep-db", action="store_true", help="保留临时数据库文件")
    args = parser.parse_args()

    tmpdir: str | None = None
    if args.db_url:
        db_url = args.db_url
    else:
        tmpdir = tempfile.mkdtemp(prefix="quamm-patrol-")
        db_url = f"sqlite:///{tmpdir}/patrol_demo.db"

    configure(db_url)
    init_db()
    session = _session()
    try:
        seed_product_types(session)
    finally:
        session.close()

    # 显式使用 mock 后端，保证脚本与 settings 默认值一致、可独立复现。
    set_prober(MockProber())
    settings.probe_timeout_seconds = 3
    settings.allow_apply_when_suspect = False

    print(f"数据库: {db_url}")
    print(f"mock 设备: 127.0.0.1:{BASE_PORT}-{BASE_PORT + COUNT - 1}   后端: mock")

    devices = _start_devices()
    try:
        ids = _register_all(devices)
        print(f"已注册 {len(ids)} 台设备，初始状态 available")

        # ---------- 阶段 1：全部在线 ----------
        run1 = _run_patrol()
        run1, dash1 = _snapshot("阶段1 全部在线")
        _check(run1.l3_count == COUNT, f"l3={run1.l3_count}（期望 {COUNT}）")
        _check(dash1["available"] == COUNT, f"dashboard available={dash1['available']}（期望 {COUNT}）")

        # ---------- 阶段 2：关掉 5 台，单轮 suspect ----------
        for dev in devices[:OFFLINE_COUNT]:
            dev.stop()
        run2 = _run_patrol()
        run2, dash2 = _snapshot(f"阶段2 关闭 {OFFLINE_COUNT} 台")
        _check(run2.suspect_count == OFFLINE_COUNT, f"suspect={run2.suspect_count}（期望 {OFFLINE_COUNT}）")
        _check(
            dash2["available"] == COUNT - OFFLINE_COUNT,
            f"dashboard available={dash2['available']}（期望 {COUNT - OFFLINE_COUNT}）",
        )
        _check(dash2["suspect"] == OFFLINE_COUNT, f"dashboard suspect={dash2['suspect']}（期望 {OFFLINE_COUNT}）")
        rows2 = _device_rows(ids[:OFFLINE_COUNT])
        _check(
            all(status == DeviceStatus.AVAILABLE and health != HealthLevel.L3 for _, status, health in rows2),
            "掉线 5 台仍 available 但 health 降级（不可申请）",
        )

        # ---------- 阶段 3：仍关着，连续 2 轮 offline + notice ----------
        run3 = _run_patrol()
        run3, dash3 = _snapshot("阶段3 连续第 2 轮失败")
        _check(run3.offline_count == OFFLINE_COUNT, f"offline={run3.offline_count}（期望 {OFFLINE_COUNT}）")
        _check(
            dash3["available"] == COUNT - OFFLINE_COUNT,
            f"dashboard available={dash3['available']}（期望 {COUNT - OFFLINE_COUNT}）",
        )
        rows3 = _device_rows(ids[:OFFLINE_COUNT])
        _check(all(status == DeviceStatus.OFFLINE for _, status, _ in rows3), "掉线 5 台已判 offline")
        session = _session()
        try:
            notice_count = (
                session.query(func.count(Notice.notice_id))
                .filter(
                    Notice.device_id.in_(ids[:OFFLINE_COUNT]),
                    Notice.event_type == "device_offline",
                )
                .scalar()
                or 0
            )
        finally:
            session.close()
        _check(notice_count == OFFLINE_COUNT, f"device_offline notice={notice_count}（期望 {OFFLINE_COUNT}）")

        # ---------- 阶段 4：重新开机，恢复回池 ----------
        for dev in devices[:OFFLINE_COUNT]:
            dev.start()
        run4 = _run_patrol()
        run4, dash4 = _snapshot("阶段4 恢复 5 台")
        _check(run4.l3_count == COUNT, f"l3={run4.l3_count}（期望 {COUNT}）")
        _check(dash4["available"] == COUNT, f"dashboard available={dash4['available']}（期望 {COUNT}）")
        rows4 = _device_rows(ids[:OFFLINE_COUNT])
        _check(
            all(status == DeviceStatus.AVAILABLE and health == HealthLevel.L3 for _, status, health in rows4),
            "恢复 5 台回到 available / L3",
        )

        # ---------- 汇总 ----------
        session = _session()
        try:
            runs = session.query(PatrolRun).order_by(PatrolRun.started_at).all()
            notice_total = session.query(func.count(Notice.notice_id)).scalar() or 0
        finally:
            session.close()
        print("\n=== 四轮 run 汇总 ===")
        for i, r in enumerate(runs, start=1):
            print(
                f"  run{i}: scanned={r.scanned_count} l3={r.l3_count} "
                f"suspect={r.suspect_count} offline={r.offline_count}"
            )
        print(f"  /api/patrol/runs 条数 = {len(runs)}；/api/notices 条数 = {notice_total}")
        _check(len(runs) == 4, f"patrol run 条数={len(runs)}（期望 4）")
        _check(notice_total >= OFFLINE_COUNT, f"notice 条数={notice_total}（至少 {OFFLINE_COUNT}）")
    finally:
        for dev in devices:
            dev.stop()
        if tmpdir and not args.keep_db:
            shutil.rmtree(tmpdir, ignore_errors=True)

    print()
    if _FAILURES:
        print(f"结果: FAIL（{len(_FAILURES)} 项断言未通过）")
        return 1
    print(f"结果: PASS（{datetime.now().isoformat(timespec='seconds')}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
