#!/usr/bin/env python3
"""设备默认名数据修正脚本（Redmine #8436）。

- 默认 --dry-run，只有显式 --apply 才写库；
- --map TYPE_CODE=NEW_NAME 修改 product_type.display_name；
- --rename DEVICE_ID=NEW_NAME 精确改名；
- 自动扫描 display_name == f"{model_class}-{position_code}" 的设备：
  该类型 display_name 非空且不等于 type_code 时，候选改名为类型 display_name，否则跳过；
- --apply 前把将要变更的原值导出为 JSON 备份（devices_display_names_backup_*.json）。

不碰其它自定义名称，不删除设备。示例：

  python scripts/fix_device_display_names.py --db /data/tres.db --map esl2_skw=彩屏价签_V2
  python scripts/fix_device_display_names.py --db /data/tres.db --apply
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import configure, get_session_factory, init_db  # noqa: E402
from app.models import Device, ProductType  # noqa: E402

MAP_RE = re.compile(r"^[^=]+=.+$")


def parse_pairs(values: list[str], what: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values or []:
        if not MAP_RE.match(v):
            raise SystemExit(f"非法 {what} 格式：{v!r}（应为 KEY=NEW_NAME）")
        key, _, name = v.partition("=")
        name = name.strip()
        if not name:
            raise SystemExit(f"{what} {key} 的新名称为空")
        out[key.strip()] = name[:128]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", required=True, help="SQLite 数据库路径（sqlite:/// 前缀可省）")
    ap.add_argument("--map", action="append", default=[], metavar="TYPE_CODE=NEW_NAME",
                    help="修改设备类型 display_name")
    ap.add_argument("--rename", action="append", default=[], metavar="DEVICE_ID=NEW_NAME",
                    help="精确修改指定设备 display_name")
    ap.add_argument("--apply", action="store_true", help="真正写库（缺省 dry-run 只打印）")
    args = ap.parse_args()

    url = args.db if args.db.startswith("sqlite:///") else f"sqlite:///{args.db}"
    configure(url)
    init_db()
    session = get_session_factory()()

    type_map = parse_pairs(args.map, "--map")
    rename_map = parse_pairs(args.rename, "--rename")
    if not type_map and not rename_map and not args.apply:
        print("提示：未给 --map/--rename 时仅做自动扫描；默认 dry-run，加 --apply 才写库")

    changes: list[dict] = []  # 统一收集：kind/key/before/after

    # 1) 类型改名
    for code, new_name in type_map.items():
        pt = session.get(ProductType, code)
        if pt is None:
            print(f"[skip] 类型 {code} 不存在")
            continue
        before = pt.display_name
        if before == new_name:
            print(f"[skip] 类型 {code} display_name 已是 {new_name!r}")
            continue
        changes.append({"kind": "product_type", "key": code, "before": before, "after": new_name})

    # 2) 设备精确改名
    for did, new_name in rename_map.items():
        d = session.get(Device, did)
        if d is None:
            print(f"[skip] 设备 {did} 不存在")
            continue
        if d.display_name == new_name:
            print(f"[skip] 设备 {did} 名称已是 {new_name!r}")
            continue
        changes.append({"kind": "device", "key": did, "before": d.display_name, "after": new_name})

    # 3) 自动扫描技术代号设备（dry-run 扫描、apply 时也扫描，before 以写库前为准）。
    #    类型本次将被 --map 改名的，自动扫描按新名称取候选，保证一条命令内类型与设备一起改。
    type_rows = {p.type_code: p for p in session.query(ProductType).all()}
    for d in session.query(Device).all():
        if not d.model_class or d.position_code is None:
            continue
        tech = f"{d.model_class}-{d.position_code}"
        if (d.display_name or "") != tech:
            continue
        pt = type_rows.get(d.model_class)
        if pt is None:
            continue
        # 优先 --map 给的新名称；未给的类型用库内 display_name
        prod = type_map.get(d.model_class) or (pt.display_name or "").strip()
        if not prod or prod == pt.type_code or prod == tech:
            continue
        changes.append({"kind": "device_auto", "key": d.device_id,
                        "before": d.display_name, "after": prod})

    if not changes:
        print("没有需要修改的条目。")
        return 0

    print(f"\n共 {len(changes)} 条变更：")
    for c in changes:
        print(f"  [{c['kind']}] {c['key']}: {c['before']!r} -> {c['after']!r}")

    if not args.apply:
        print("\ndry-run：未写库。确认无误后加 --apply 执行。")
        return 0

    # apply 前备份原值
    backup_name = f"devices_display_names_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    backup = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "db": url,
        "changes": changes,
    }
    Path(backup_name).write_text(
        json.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n已导出备份：{backup_name}")

    for c in changes:
        if c["kind"] == "product_type":
            session.get(ProductType, c["key"]).display_name = c["after"]
        else:
            session.get(Device, c["key"]).display_name = c["after"]
    session.commit()
    print(f"已写入 {len(changes)} 条变更。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
