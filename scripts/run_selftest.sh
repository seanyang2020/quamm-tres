#!/bin/bash
# Quamm TRES 系统自测试（v0.18 落档）。
# 用途：在任意改动后一键跑全量 pytest，输出通过/失败汇总；退出码即 pytest 退出码。
# 说明：物理闸门（真实 adb 删锁/断连）在单元测试中由 conftest 自动关闭，
#       因此本测试不依赖真实设备，可在本机/CI 直接运行。
set -u
cd "$(dirname "$0")/.." || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
echo "=== Quamm TRES self-test: $(date '+%F %T') ==="
"$PY" -m pytest -q "$@"
rc=$?
echo "=== self-test finished, exit=$rc ==="
exit $rc
