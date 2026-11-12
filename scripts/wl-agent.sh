#!/bin/sh
# wl-agent：日志机侧 WireLog 客户端（busybox ash）。
# 从 uboot-env 读 node_id/token/hmac_key，周期注册/心跳；经串口 shell 实测
# 被测设备 MAC/IP 并上报，服务端按 MAC 自动绑定（R-7/ADR-7）；
# 用 wlhmac 校验响应 HMAC 与时间窗（ADR-6）。
#
# 部署: /system/app/serialhub/wl-agent.sh（S99 用 setsid 拉起）
# 依赖: fw_printenv、busybox wget/microcom、/system/app/serialhub/wlhmac
BASE_URL="${WL_BASE_URL:-http://172.16.10.25:9100}"
INTERVAL="${WL_INTERVAL:-60}"
HMAC_BIN=/system/app/serialhub/wlhmac
WINDOW=600
TMPD=/tmp/wl.$$
mkdir -p "$TMPD"
trap 'rm -rf "$TMPD"' EXIT

log() { echo "wl-agent: $*"; }

read_env() {
  fw_printenv 2>/dev/null | while IFS='=' read -r k v; do
    case "$k" in
      wl_node_id) echo "ID=$v" ;;
      wl_node_token) echo "TOK=$v" ;;
      wl_hmac_key) echo "HKEY=$v" ;;
    esac
  done
}

# 经串口 shell 实测：stdout 输出 "<mac>|<ip>"（拿不到则为空）。失败自动重试一次。
probe_once() {
  tty="$1"; cap="$2"; lck="$3"
  : > "$cap"; rm -f "$lck"
  microcom -s 115200 "$tty" >>"$cap" 2>&1 &
  mp=$!
  sleep 0.5
  printf '\r' > "$tty"; sleep 0.4
  # 拆成短命令，避免长命令在串口终端被回显/换行截断
  printf 'echo BEG\r' > "$tty"; sleep 0.4
  printf 'cat /sys/class/net/wlan0/address\r' > "$tty"; sleep 0.5
  printf "ifconfig wlan0 | grep -o 'inet addr:[0-9.]*' | cut -d: -f2\r" > "$tty"; sleep 0.8
  printf 'echo END\r' > "$tty"; sleep 0.4
  kill "$mp" 2>/dev/null
  pkill microcom 2>/dev/null
  rm -f "$lck"
  sed -n '/BEG/,/END/p' "$cap"
}

probe_port() {
  tty="$1"
  cap="$TMPD/cap.$(basename "$tty")"
  lck="/var/lock/LCK..$(basename "$tty")"
  block=$(probe_once "$tty" "$cap" "$lck")
  # 无完整定界块则重试一次
  if ! printf '%s' "$block" | grep -q END; then
    sleep 0.5
    block=$(probe_once "$tty" "$cap" "$lck")
  fi
  # 不用 {n} 区间量词（busybox 在管道内有兼容问题），显式重复
  mac=$(printf '%s' "$block" |
        grep -o '[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]:[0-9A-Fa-f][0-9A-Fa-f]' | head -1)
  ip=$(printf '%s' "$block" |
       grep -o '[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*' |
       grep -v '^127\.' | head -1)
  echo "$mac|$ip"
}

# 组装 ports JSON（遍历 /dev/ttyUSB*，带实测 MAC/IP）
ports_json() {
  first=1
  printf '['
  slot=1
  for tty in /dev/ttyUSB*; do
    [ -e "$tty" ] || continue
    [ $first -eq 1 ] || printf ','
    first=0
    probe=$(probe_port "$tty")
    mac=${probe%%|*}; ip=${probe##*|}
    reachable=false
    [ -n "$mac" ] && reachable=true
    printf '{"slot":%d,"tty":"%s","baud":115200,"shell_reachable":%s' \
      "$slot" "$tty" "$reachable"
    [ -n "$mac" ] && printf ',"shell_mac":"%s"' "$mac"
    [ -n "$ip" ] && printf ',"shell_ip":"%s"' "$ip"
    printf '}'
    slot=$((slot + 1))
  done
  printf ']'
}

body() {
  IP=$(ip addr show wlan0 2>/dev/null | awk '/inet /{print $2}' | cut -d/ -f1)
  [ -n "$IP" ] || IP=$(ifconfig wlan0 2>/dev/null | awk '/inet addr/{print $2}' | cut -d: -f2)
  VER=$(cat /system/app/serialhub/VERSION 2>/dev/null)
  PORTS=$(ports_json)
  NONCE=$(cat /proc/sys/kernel/random/uuid 2>/dev/null | tr -d '-')
  [ -n "$NONCE" ] || NONCE=$(date +%s)$$
  # 上报本机 serialhub raw TCP 端口与登录 token，供 TRES 经 uart 通道探测
  RPORT=9001
  # 主 token 读取顺序与 serialhub 一致：/data/serialhub/token（首启自举落盘，每机唯一）
  # 优先，回退 /etc/serialhub/token（旧镜像遗留）。
  # 主 token 仅作 TRES 管理通道凭据上报：TRES 用它写/撤派生票（rt_ 标记租约），
  # 永不下发给申请设备的 agent。agent 只拿派生票。
  LTOK=""
  for f in /data/serialhub/token /etc/serialhub/token; do
    [ -r "$f" ] || continue
    LTOK=$(cat "$f" 2>/dev/null | tr -d '\r\n')
    [ -n "$LTOK" ] && break
  done
  printf '{"ip":"%s","sw_version":"%s","nonce":"%s","caps":{"ports":1,"raw_tcp_port":%d,"local_token":"%s"},"ports":%s}' \
    "$IP" "$VER" "$NONCE" "$RPORT" "$LTOK" "$PORTS"
}

post() {
  path="$1"; payload="$2"; tok="$3"; hkey="$4"
  hdrf="$TMPD/hdr"; bodyf="$TMPD/body"
  wget -S -T 15 -O "$bodyf" --post-data "$payload" \
    --header "Authorization: Bearer $tok" \
    --header "Content-Type: application/json" \
    "$BASE_URL/api/wirelog/$path" 2>"$hdrf" || return 1

  ts=$(grep -i 'X-WL-Ts:' "$hdrf" | head -1 | sed 's/^[^:]*://;s/^[[:space:]]*//;s/[[:space:]]*$//')
  nonce=$(grep -i 'X-WL-Nonce:' "$hdrf" | head -1 | sed 's/^[^:]*://;s/^[[:space:]]*//;s/[[:space:]]*$//')
  sig=$(grep -i 'X-WL-Sig:' "$hdrf" | head -1 | sed 's/^[^:]*://;s/^[[:space:]]*//;s/[[:space:]]*$//')
  [ -n "$ts" ] && [ -n "$sig" ] || { log "响应缺少签名头，拒绝"; return 1; }

  if "$HMAC_BIN" "$hkey" "$ts" "$nonce" "$sig" "$WINDOW" "$bodyf"; then
    cat "$bodyf"
    return 0
  fi
  log "HMAC 校验失败，拒绝该响应"
  return 1
}

while :; do
  eval "$(read_env)"
  [ -n "$ID" ] && [ -n "$TOK" ] && [ -n "$HKEY" ] && break
  log "uboot-env 缺 wl_node_id/token/hmac_key，30s 后重试"
  sleep 30
done
log "node=$ID"

while :; do
  PAYLOAD=$(body)
  if [ "$WL_DRY_RUN" = "1" ]; then
    printf '%s\n' "$PAYLOAD"
    break
  fi
  if [ "$REGISTERED" != "1" ]; then
    R=$(post register "$PAYLOAD" "$TOK" "$HKEY") && REGISTERED=1 && log "register verified: $R"
  else
    R=$(post heartbeat "$PAYLOAD" "$TOK" "$HKEY") && log "heartbeat verified"
  fi
  sleep "$INTERVAL"
done
