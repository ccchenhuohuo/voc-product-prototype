#!/usr/bin/env bash
# VOC 机会看板启动脚本（在 ulanzicloud 上执行）
# 用法：bash /home/sdy/voc-system/scripts/start.sh
# 前置条件：/home/sdy/voc-system/.venv 已建且装好依赖；VOC_PG_* 环境变量可从
#   $VOC_ENV_FILE（默认 /home/sdy/voc-analytics/.env）读到；目标端口未被占用。
#
# 为什么需要这个脚本：2026-08-17 排查时发现应用是在某次交互式 shell 里 export
# 变量后直接起的 uvicorn——目录里既无 .env 也无启动脚本，进程一旦退出，
# 没有任何地方记录着该怎么把它拉起来。用 setsid 完全脱离调用方会话，
# 避免 ssh 断开或 pkill 误伤时连带把服务带走。
set -euo pipefail

APP_DIR="${VOC_SYSTEM_DIR:-/home/sdy/voc-system}"
ENV_FILE="${VOC_ENV_FILE:-/home/sdy/voc-analytics/.env}"
PORT="${VOC_BIND_PORT:-8090}"
LOG="${VOC_SYSTEM_LOG:-/tmp/voc_system.log}"

# 绑定地址运行时取，不写进仓库——本机 Tailscale 地址即内网可达地址。
# 拿不到就退回回环，宁可只有本机能访问，也不把 IP 记进版本库。
HOST="${VOC_BIND_HOST:-$(tailscale ip -4 2>/dev/null | head -1)}"
HOST="${HOST:-127.0.0.1}"

[ -d "$APP_DIR/.venv" ] || { echo "!! 缺少 $APP_DIR/.venv" >&2; exit 2; }
[ -r "$ENV_FILE" ]      || { echo "!! 读不到环境文件 $ENV_FILE" >&2; exit 3; }

cd "$APP_DIR"

# 只取 VOC_PG_* 传给进程，不把无关变量（含其他项目的密钥）带进来。
mapfile -t PGVARS < <(grep -E '^VOC_PG_[A-Z_]+=' "$ENV_FILE" | grep -v '^#')
[ "${#PGVARS[@]}" -gt 0 ] || { echo "!! $ENV_FILE 里没有 VOC_PG_* 变量" >&2; exit 4; }

# 用 [u] 括号写法，避免 pgrep/pkill 匹配到本脚本自身的命令行。
# 这个坑踩过两次：直接 pkill -f "uvicorn app.main" 会连 ssh 会话一起杀掉，退出码 255。
if pgrep -f "[u]vicorn app.main" >/dev/null 2>&1; then
  echo "== 停止旧进程 =="
  pkill -f "[u]vicorn app.main" || true
  for _ in $(seq 1 15); do
    pgrep -f "[u]vicorn app.main" >/dev/null 2>&1 || break
    sleep 1
  done
  pgrep -f "[u]vicorn app.main" >/dev/null 2>&1 && { echo "!! 旧进程未退出" >&2; exit 5; }
fi

echo "== 启动 $HOST:$PORT =="
setsid env "${PGVARS[@]}" \
  .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" \
  --proxy-headers --forwarded-allow-ips=127.0.0.1 \
  >"$LOG" 2>&1 </dev/null &
disown || true

for _ in $(seq 1 30); do
  sleep 1
  if curl -sf -o /dev/null --max-time 5 "http://${HOST}:${PORT}/iter"; then
    echo "   就绪：http://${HOST}:${PORT}/iter    日志 $LOG"
    exit 0
  fi
done

echo "!! 30 秒内未就绪，日志尾部：" >&2
tail -20 "$LOG" >&2
exit 6
