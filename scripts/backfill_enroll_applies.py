#!/usr/bin/env python3
"""为 token 直连（无 apply）的节点回填 wl_client_apply approved 行（幂等）。

用法：
  .venv/bin/python scripts/backfill_enroll_applies.py --dry-run
  .venv/bin/python scripts/backfill_enroll_applies.py
  .venv/bin/python scripts/backfill_enroll_applies.py --db-url sqlite:///./quamm_res.db
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import settings  # noqa: E402
from app.db import configure, get_session_factory, init_db  # noqa: E402
from app.domain import comres_apply  # noqa: E402
from app.migrations import migrate  # noqa: E402
from app.models import WireLogClientApply, WireLogNode  # noqa: E402


def candidates(session):
    """token_prefix 非空、未退役、且尚无 issued_node_id 指向的 apply。"""
    applied = {
        r.issued_node_id
        for r in session.query(WireLogClientApply.issued_node_id)
        .filter(WireLogClientApply.issued_node_id.isnot(None))
        .all()
        if r.issued_node_id
    }
    nodes = (
        session.query(WireLogNode)
        .filter(WireLogNode.token_prefix.isnot(None))
        .filter(WireLogNode.token_prefix != "")
        .filter(WireLogNode.status != "retired")
        .all()
    )
    return [n for n in nodes if n.node_id not in applied]


def run(*, dry_run: bool = False) -> dict:
    session = get_session_factory()()
    try:
        migrate(session)
        nodes = candidates(session)
        created = []
        for n in nodes:
            created.append({
                "node_id": n.node_id,
                "display_name": n.display_name,
                "token_prefix": n.token_prefix,
            })
            if not dry_run:
                comres_apply.ensure_enroll_apply(
                    session, n, operator="backfill_enroll_applies")
        if not dry_run and created:
            session.commit()
        # 二次扫描：确认幂等（再跑应无候选）
        remaining = 0 if dry_run else len(candidates(session))
        return {
            "dry_run": dry_run,
            "candidates": len(created),
            "created": created,
            "remaining_after": remaining,
        }
    finally:
        session.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="回填 enroll 预置 token 的 apply 行")
    p.add_argument("--dry-run", action="store_true", help="只列出候选，不写库")
    p.add_argument("--db-url", default=None, help="覆盖 RES_DATABASE_URL / settings")
    args = p.parse_args(argv)

    url = args.db_url or settings.database_url
    configure(url)
    init_db()
    result = run(dry_run=args.dry_run)
    print(f"db={url}")
    print(f"dry_run={result['dry_run']} candidates={result['candidates']} "
          f"remaining_after={result['remaining_after']}")
    for item in result["created"]:
        print(f"  + {item['node_id']}  {item['display_name']}  "
              f"prefix={item['token_prefix']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
