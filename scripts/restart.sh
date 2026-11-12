#!/bin/bash
# Quamm TRES 部署/重启（systemd --user 托管）
# 用法：bash scripts/restart.sh
set -e
cd "$(dirname "$0")/.."
TRES_PORT="${TRES_PORT:-9100}"

systemctl --user restart quamm-tres.service
sleep 4
systemctl --user restart quamm-worker.service
sleep 3

echo "== units =="
systemctl --user is-active quamm-tres.service quamm-worker.service
echo "== health =="
curl -s "http://127.0.0.1:${TRES_PORT}/api/health"
echo
