#!/bin/bash
# Quamm TRES 离线代码升级（部署机到不了 GitHub 时用 git bundle）
#
# 背景：生产机（如 172.16.1.201）不能出网访问 GitHub，本仓库用 double remote：
#   origin  = https://github.com/seanyang2020/quamm-tres.git  （记录真上游，仅作语义）
#   bundle  = file://<部署机上的 bundle 路径>                  （实际 fetch 来源）
#
# 用法：
#   bash scripts/deploy-git.sh                      # 默认 $HOME/quamm-tres.bundle
#   bash scripts/deploy-git.sh /path/to/tres.bundle
#
# 注意：
#   - 只更新「被 git 跟踪的代码」，不会动 .env / var/ / *.db / .venv / web/node_modules；
#   - web/dist 属于构建产物（被 .gitignore 忽略），需另行同步或在本机构建；
#   - 回滚：git checkout -f <旧 commit> 然后 bash scripts/restart.sh
set -e
cd "$(dirname "$0")/.."
TRES_PORT="${TRES_PORT:-9100}"
BUNDLE="${1:-$HOME/quamm-tres.bundle}"

if [ ! -f "$BUNDLE" ]; then
  echo "ERROR: bundle 不存在: $BUNDLE" >&2
  exit 1
fi

OLD=$(git rev-parse --short HEAD 2>/dev/null || echo "none")
echo "== fetch =="
git fetch -q "$BUNDLE" "refs/heads/main:refs/remotes/bundle/main" -f
NEW=$(git rev-parse --short HEAD)
git checkout -q -f -B main bundle/main
NEW=$(git rev-parse --short HEAD)
echo "code: $OLD -> $NEW  $(git show -s --format=%s HEAD)"

echo "== restart =="
systemctl --user restart quamm-tres.service
sleep 5
systemctl --user restart quamm-worker.service
sleep 3

echo "== units =="
systemctl --user is-active quamm-tres.service quamm-worker.service
echo "== health =="
curl -s "http://127.0.0.1:${TRES_PORT}/api/health"
echo
echo "== VERSION =="
cat VERSION
