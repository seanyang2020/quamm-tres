#!/usr/bin/env python3
"""手工回收 Quamm TRES 缓存（DB 源包/解压目录 + 刷机 downloads/logs/tmp）。

自动回收已内建在流程里（worker 每 6 小时 + DB 升级结束即清 + 刷机 job 结束即清），
本脚本用于运维手工核查与立即释放空间。

用法（在项目根执行）：
  .venv/bin/python scripts/prune_cache.py            # 按保留策略清理
  .venv/bin/python scripts/prune_cache.py --dry-run  # 只统计会删什么，不动文件
  .venv/bin/python scripts/prune_cache.py --stats    # 只看占用与策略，不删
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser(description="Quamm TRES 缓存回收")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不删除")
    ap.add_argument("--stats", action="store_true", help="只打印占用与策略")
    args = ap.parse_args()

    from app import cache_prune

    if args.stats:
        print(json.dumps(cache_prune.cache_stats(), ensure_ascii=False, indent=2))
        return 0

    result = cache_prune.prune_all(dry_run=args.dry_run)
    prefix = "[dry-run] " if args.dry_run else ""
    print(f"{prefix}合计释放 {result['freed_human']}（{result['before_total_human']} -> {result['after_total_human']}）")
    for part in ("db_source", "flash"):
        r = result[part]
        print(f"  {part}: {r['deleted_count']} 项 / {r['freed_human']}")
        for item in r["deleted"][:20]:
            print(f"    - [{item['kind']}] {item['path']}")
        if len(r["deleted"]) > 20:
            print(f"    ... 其余 {len(r['deleted']) - 20} 项略")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
