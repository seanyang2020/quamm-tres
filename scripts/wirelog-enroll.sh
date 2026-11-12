#!/bin/sh
# WireLog 发放：把中心签发的 node_id / node_token / hmac_key 写入设备 uboot-env。
# 运行在有 adb 的机器（如 172.16.1.202）。
#
# 用法:
#   wirelog-enroll.sh <adb_address> <node_id> <node_token> [hmac_key]
#
# 先经 Web「WireLog 发放」或 MCP wl_enroll 生成，本脚本只负责落盘。
set -e

ADB_ADDR="$1"; NODE_ID="$2"; TOKEN="$3"; HKEY="$4"
if [ -z "$ADB_ADDR" ] || [ -z "$NODE_ID" ] || [ -z "$TOKEN" ]; then
  echo "usage: $0 <adb_address> <node_id> <node_token> [hmac_key]" >&2
  exit 2
fi

adb connect "$ADB_ADDR" >/dev/null

# 锁目录在 tmpfs 上可能不存在，先建；用 -l /tmp 兜底。
adb -s "$ADB_ADDR" shell 'mkdir -p /var/lock'

echo "[1/3] 写 wl_node_id / wl_node_token / wl_hmac_key"
adb -s "$ADB_ADDR" shell "fw_setenv -l /tmp wl_node_id $NODE_ID"
adb -s "$ADB_ADDR" shell "fw_setenv -l /tmp wl_node_token $TOKEN"
if [ -n "$HKEY" ]; then
  adb -s "$ADB_ADDR" shell "fw_setenv -l /tmp wl_hmac_key $HKEY"
fi

echo "[2/3] 回读校验"
OUT=$(adb -s "$ADB_ADDR" shell 'fw_printenv wl_node_id wl_node_token wl_hmac_key')
echo "$OUT"
echo "$OUT" | grep -q "wl_node_id=$NODE_ID"
echo "$OUT" | grep -q "wl_node_token=$TOKEN"
if [ -n "$HKEY" ]; then echo "$OUT" | grep -q "wl_hmac_key=$HKEY"; fi

echo "[3/3] 完成，重启 wl-agent 后即注册（若已在跑会在下个心跳自动生效）"
adb -s "$ADB_ADDR" shell 'mkdir -p /var/lock'
echo "ENROLL_OK"
