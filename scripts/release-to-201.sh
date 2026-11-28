#!/bin/bash
# Quamm TRES 单源发布：本地仓 -> 201 /opt/quamm-tres（代码只读、不碰运行态）
#
# 用法：
#   bash scripts/release-to-201.sh --dry-run     # 只读预演：打印与 /opt 现有版本的差异
#   bash scripts/release-to-201.sh               # 构建 + 上传 + 安装到新版本目录（不切换）
#   bash scripts/release-to-201.sh --activate    # 再切换 current + 重启 + 健康检查
#   bash scripts/release-to-201.sh --build-only  # 只构建 web
#
# 环境变量：
#   TRES_HOST=dbtest@172.16.1.201
#   TRES_SUDO_PW=<sudo 密码>   不设则安装步骤会要求交互输入（ssh -t）
#
# 约束：只同步代码与 web/dist；DB、日志、venv、/etc 环境文件一律不动。
set -euo pipefail
cd "$(dirname "$0")/.."
HOST=${TRES_HOST:-dbtest@172.16.1.201}
VER=$(tr -d '[:space:]' < VERSION)
DEST="/opt/quamm-tres/releases/${VER}"
STAGE="/tmp/quamm-tres-staging-${VER}"
MODE=sync
for a in "$@"; do
  case "$a" in
    --dry-run) MODE=dry ;;
    --activate) MODE=activate ;;
    --build-only) MODE=build ;;
    *) echo "unknown arg: $a" >&2; exit 2;;
  esac
done

echo "== version ${VER}  target ${HOST}:${DEST}"

if [ "$MODE" != dry ]; then
  if [ ! -d web/node_modules ]; then echo "ERROR: web/node_modules 缺失，先在 web/ 跑 npm ci" >&2; exit 1; fi
  ( cd web && ./node_modules/.bin/vite build >/tmp/tres-vite.log 2>&1 ) || { tail -20 /tmp/tres-vite.log; exit 1; }
  echo "built: $(ls -1 web/dist/assets/index-*.js | tail -1)"
fi
[ "$MODE" = build ] && exit 0

RSYNC=(rsync -a --exclude '.git/' --exclude '.venv/' --exclude 'var/' --exclude '*.db' --exclude '*.db-*'
  --exclude '*.db.bak*' --exclude 'web/node_modules/' --exclude '__pycache__/' --exclude '.pytest_cache/'
  --exclude '.env' --exclude 'scripts/.demo_*' --exclude 'devices_display_names_backup_*.json' --exclude '*.log')

if [ "$MODE" = dry ]; then
  "${RSYNC[@]}" --dry-run --itemize-changes ./ "$HOST:$DEST/"
  echo "(dry-run only)"
  exit 0
fi

echo "== upload to staging"
ssh "$HOST" "rm -rf $STAGE && mkdir -p $STAGE"
"${RSYNC[@]}" ./ "$HOST:$STAGE/"

echo "== install (needs sudo)"
cat > /tmp/tres-install.sh <<EOS
#!/bin/bash
set -e
rm -rf "$DEST"
mkdir -p "$DEST"
cp -a "$STAGE/." "$DEST/"
chown -R root:root "$DEST"
find "$DEST" -type d -exec chmod 0555 {} +
find "$DEST" -type f -exec chmod 0444 {} +
ln -sfn "$DEST" /opt/quamm-tres/current
ls -l /opt/quamm-tres/current
EOS
scp -q /tmp/tres-install.sh "$HOST:/tmp/tres-install.sh"
if [ -n "${TRES_SUDO_PW:-}" ]; then
  printf '%s\n' "$TRES_SUDO_PW" | ssh "$HOST" "sudo -S bash /tmp/tres-install.sh"
else
  ssh -t "$HOST" "sudo bash /tmp/tres-install.sh"
fi

if [ "$MODE" != activate ]; then
  echo "已安装到 ${DEST}（current 已指向）。切换：bash $0 --activate"
  exit 0
fi

echo "== activate"
if [ -n "${TRES_SUDO_PW:-}" ]; then
  printf '%s\n' "$TRES_SUDO_PW" | ssh "$HOST" "sudo -S systemctl restart quamm-tres.service quamm-worker.service"
else
  ssh -t "$HOST" "sudo systemctl restart quamm-tres.service quamm-worker.service"
fi
sleep 6
curl -s --max-time 8 "http://172.16.1.201:9100/api/health"; echo
ssh "$HOST" "systemctl is-active quamm-tres.service quamm-worker.service; readlink -f /opt/quamm-tres/current"
echo "回滚：把 current 指回旧版本 + systemctl restart"
