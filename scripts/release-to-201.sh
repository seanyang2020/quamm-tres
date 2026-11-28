#!/bin/bash
# Quamm TRES 单源发布：本地仓 -> 201 releases/<ver>
# 只更新代码（含 web/dist），不碰 .venv / var / *.db / .env 之外的运行态。
#
# 用法：
#   bash scripts/release-to-201.sh --dry-run          # 只打印将同步的差异
#   bash scripts/release-to-201.sh                    # 同步到 ~/releases/quamm-res-<VERSION>，不切换
#   bash scripts/release-to-201.sh --activate         # 同步 + 改 unit WorkingDirectory + 重启 + 健康检查
#   bash scripts/release-to-201.sh --build-only       # 只在本机构建 web
#
# 环境变量（可覆盖）：
#   TRES_HOST=dbtest@172.16.1.201
#   TRES_DB_URL=sqlite:////home/dbtest/quamm-res/quamm_res.db   （沿用现有 DB，不迁移）
#   TRES_MCP_URL=http://172.16.1.201:9100/mcp/
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
HOST=${TRES_HOST:-dbtest@172.16.1.201}
VER=$(tr -d '[:space:]' < VERSION)
DEST="releases/quamm-res-${VER}"
MODE=sync
for a in "$@"; do
  case "$a" in
    --dry-run) MODE=dry ;;
    --activate) MODE=activate ;;
    --build-only) MODE=build ;;
    *) echo "unknown arg: $a" >&2; exit 2;;
  esac
done

echo "== version: $VER  target: $HOST:~/$DEST =="

if [ "$MODE" != "dry" ]; then
  echo "== build web =="
  if [ ! -d web/node_modules ]; then echo "ERROR: web/node_modules 缺失，先在 web/ 跑 npm ci" >&2; exit 1; fi
  ( cd web && ./node_modules/.bin/vite build >/tmp/tres-vite.log 2>&1 ) || { tail -20 /tmp/tres-vite.log; exit 1; }
  ls -1 web/dist/assets/index-*.js | tail -1
fi
[ "$MODE" = build ] && exit 0

echo "== rsync code (no venv/var/db/.git/node_modules) =="
RSYNC=(rsync -a
  --exclude '.git/' --exclude '.venv/' --exclude 'var/' --exclude '*.db' --exclude '*.db-*'
  --exclude '*.db.bak*' --exclude 'web/node_modules/' --exclude '__pycache__/' --exclude '.pytest_cache/'
  --exclude '.env' --exclude 'scripts/.demo_*' --exclude 'devices_display_names_backup_*.json' --exclude '*.log')
[ "$MODE" = dry ] && RSYNC+=( --dry-run --itemize-changes )
"${RSYNC[@]}" ./ "$HOST:$DEST/"
[ "$MODE" = dry ] && { echo "(dry-run，未写入)"; exit 0; }

echo "== write .env (沿用现有 DB，不迁移) =="
ssh "$HOST" "cat > ~/$DEST/.env <<EOF
RES_MCP_PUBLIC_URL=${TRES_MCP_URL:-http://172.16.1.201:9100/mcp/}
RES_DATABASE_URL=${TRES_DB_URL:-sqlite:////home/dbtest/quamm-res/quamm_res.db}
EOF"

echo "== remote smoke =="
ssh "$HOST" "cd ~/$DEST && ls VERSION >/dev/null && cat VERSION && test -f web/dist/index.html && echo build-ok"

if [ "$MODE" != activate ]; then
  echo "同步完成（未切换）。切换：ssh $HOST 'sed -i ...' 或重跑本脚本加 --activate"
  exit 0
fi

echo "== activate =="
ssh "$HOST" "set -e
cd ~/.config/systemd/user
for u in quamm-tres.service quamm-worker.service; do
  cp -n \$u \$u.bak-\$(date +%Y%m%d-%H%M%S)
  sed -i 's#^WorkingDirectory=.*#WorkingDirectory=/home/dbtest/$DEST#' \$u
done
systemctl --user daemon-reload
systemctl --user restart quamm-tres.service
sleep 5
systemctl --user restart quamm-worker.service
sleep 3
systemctl --user is-active quamm-tres.service quamm-worker.service
curl -s --max-time 8 http://127.0.0.1:9100/api/health; echo
"
echo "回滚：ssh $HOST \"sed -i 's#^WorkingDirectory=.*#WorkingDirectory=/home/dbtest/releases/quamm-res-<旧版本>#' ~/.config/systemd/user/quamm-*.service && systemctl --user daemon-reload && systemctl --user restart quamm-tres quamm-worker\""
