#!/usr/bin/env python3
"""校验 TRES 依赖的外部工具是否与 tools.lock.json 一致。

用法：
    python3 scripts/check-tools.py           # 人类可读表格
    python3 scripts/check-tools.py --json    # 机器可读
    python3 scripts/check-tools.py --strict  # 可选工具也算失败

退出码：0 全部通过；1 有 critical 项不通过；2 lock 文件读不到。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys

LOCK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools.lock.json")


def md5_of(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve(entry: dict) -> tuple[str | None, str]:
    """返回 (实际路径, 命中方式)。先按登记路径找，再按 PATH 找。"""
    for p in entry.get("paths", []):
        real = os.path.expanduser(p)
        if os.path.isfile(real):
            return real, "登记路径"
    name = entry.get("which")
    if name:
        found = shutil.which(name)
        if found:
            return found, "PATH"
    return None, "未找到"


def first_line(text: str) -> str:
    for line in (text or "").splitlines():
        line = line.strip()
        if line:
            return line
    return ""


def version_of(entry: dict, path: str) -> tuple[str, str]:
    tmpl = entry.get("version_cmd")
    if not tmpl:
        return "", "未配置 version_cmd"
    cmd = tmpl.replace("{path}", path)
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=25
        )
    except Exception as exc:  # noqa: BLE001 - 任何执行失败都折算成探测失败
        return "", f"执行失败：{exc}"
    out = first_line((proc.stdout or "") + (proc.stderr or ""))
    return out, f"rc={proc.returncode}"


def check_one(entry: dict) -> dict:
    name = entry.get("name", "?")
    expect = str(entry.get("version", ""))
    critical = bool(entry.get("critical", True))

    path, how = resolve(entry)
    result = {
        "name": name,
        "expect": expect,
        "path": path or "-",
        "found_via": how,
        "critical": critical,
        "problems": [],
    }
    if not path:
        result["problems"].append("未安装（登记路径与 PATH 都没有）")
        result["ok"] = False
        return result

    if not os.access(path, os.X_OK):
        result["problems"].append("不可执行")

    expect_md5 = entry.get("md5")
    if expect_md5:
        try:
            actual_md5 = md5_of(path)
        except OSError as exc:
            actual_md5 = f"读取失败：{exc}"
        result["md5"] = actual_md5
        if actual_md5 != expect_md5:
            result["problems"].append(f"md5 不符（期望 {expect_md5[:12]}… 实际 {actual_md5[:12]}…）")

    actual_version, detail = version_of(entry, path)
    result["actual"] = actual_version
    result["version_detail"] = detail
    if expect and expect not in actual_version:
        result["problems"].append(f"版本不符（期望 {expect} 实际 {actual_version or '取不到'}）")

    result["ok"] = not result["problems"]
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--strict", action="store_true", help="可选工具失败也算失败")
    args = ap.parse_args()

    try:
        with open(LOCK_PATH, "r", encoding="utf-8") as fh:
            lock = json.load(fh)
    except (OSError, ValueError) as exc:
        print(f"ERROR: 读不到 {LOCK_PATH}: {exc}", file=sys.stderr)
        return 2

    results = [check_one(e) for e in lock.get("tools", [])]
    failed_critical = [r for r in results if not r["ok"] and r["critical"]]
    failed_optional = [r for r in results if not r["ok"] and not r["critical"]]

    if args.json:
        print(json.dumps(
            {"lock": LOCK_PATH, "updated": lock.get("updated"),
             "results": results,
             "failed_critical": len(failed_critical),
             "failed_optional": len(failed_optional)},
            ensure_ascii=False, indent=2,
        ))
    else:
        print(f"tools.lock.json（{lock.get('updated')}）  {LOCK_PATH}")
        print("-" * 96)
        for r in results:
            tag = "PASS" if r["ok"] else ("FAIL" if r["critical"] else "WARN")
            actual = r.get("actual") or "-"
            print(f"[{tag:4}] {r['name']:<18} 期望 {r.get('expect') or '*':<10} 实际 {actual:<28} {r['path']}")
            for prob in r["problems"]:
                print(f"       └─ {prob}")
        print("-" * 96)
        print(f"critical 失败 {len(failed_critical)} 项，可选项缺失/不符 {len(failed_optional)} 项")

    if failed_critical:
        return 1
    if args.strict and failed_optional:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
