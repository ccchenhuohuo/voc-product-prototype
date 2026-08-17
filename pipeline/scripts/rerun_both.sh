#!/usr/bin/env bash
# 清库 + 两个生命周期并行重跑 + 统一收尾。
# 用法：bash ~/voc-analytics/scripts/rerun_both.sh
# 前置条件：~/voc-analytics 下已有 .env、.venv 与事实层数据；voc-postgres 正常；
#   百炼/LLM 凭据与配额可用。目标周当前固定为 2026-W33；
#   PER_PROC_CONCURRENCY 可调整并发。BUCKETS 默认 0（全量），若设置后实际
#   截断了桶，生成程序会在调用 LLM 前失败，绝不以子集冒充全量。
# 警告：脚本会终止匹配 run_generate.py 的进程并清空机会点层；若存在受外键保护的
# 人工决策则按设计中止。执行前应确认备份并排除其他生成任务。
#
# 两段式是正确性要求：并行阶段只做生成，汇聚/拆分/快照/放行必须
# 等老品迭代与新品创新两个生命周期都成功后再统一做一遍。
set -euo pipefail

# 有超时的子进程监督是正确性硬依赖（见 wait_with_timeout）。必须在停止进程、清库等任何
# 破坏性动作之前失败，不能等后台任务已启动后才发现系统 Bash 过旧。
if (( BASH_VERSINFO[0] < 5 ||
      (BASH_VERSINFO[0] == 5 && BASH_VERSINFO[1] < 1) )); then
  echo "!! rerun_both.sh 需要 Bash >= 5.1（当前 ${BASH_VERSION}），未做任何变更。" >&2
  exit 2
fi
cd ~/voc-analytics

# bash 的 wait 没有 -t 选项（用法只有 [-fn] [-p var] [id ...]），
# 2026-08-17 实测报 "wait: -t: invalid option" 并使整段监督失效。
# 这里用轮询实现有上限的等待：进程退出则 wait 收退出码，超时则留空
# finished_pid 让调用方走 KILL 分支。
# 定义必须在 shebang 与版本守卫之后——否则 shebang 沦为普通注释，
# 直接 ./rerun_both.sh 会落到调用方的默认 shell 上。
wait_with_timeout() {
  local pid="$1" limit="$2" pid_var="$3" rc_var="$4"
  local waited=0 rc=0
  while kill -0 "$pid" 2>/dev/null && (( waited < limit )); do
    sleep 1
    (( waited++ )) || true
  done
  if kill -0 "$pid" 2>/dev/null; then
    printf -v "$pid_var" '%s' ""      # 超时未退出
    printf -v "$rc_var"  '%s' "124"
  else
    if wait "$pid"; then rc=0; else rc=$?; fi
    printf -v "$pid_var" '%s' "$pid"
    printf -v "$rc_var"  '%s' "$rc"
  fi
}

# 所有应用环境变量都在任何停止/清库动作前加载；后续监督契约据最终值校验。
set -a; . ./.env; set +a

# 全局闸门 _GATE 是【进程内】的信号量，两个进程各持一份，所以每个进程分一半。
# 2026-08-17 复测拐点由 64 上移到 96（128 起连接被对端掐断），故每进程 48。
PER_PROC_CONCURRENCY="${PER_PROC_CONCURRENCY:-48}"
RUN_ID="${RUN_ID:-gen_2026W33_$(date -u +%Y%m%dT%H%M%SZ)_$$}"
# 两个 Python 进程共享首个 fatal；文件路径按本次 supervisor PID 隔离。
# llm.py 用原子发布保留首因，另一进程每次请求前都会检查。
VOC_LLM_CIRCUIT_FILE="${VOC_LLM_CIRCUIT_FILE:-/tmp/voc_llm_circuit_$$}"
if [[ -e "$VOC_LLM_CIRCUIT_FILE" ]]; then
  echo "!! 共享熔断文件已存在，拒绝在状态不明时清库：$VOC_LLM_CIRCUIT_FILE" >&2
  exit 2
fi
# llm.py 用同目录临时文件 + hard link 原子发布首因。现在先验证目录可写且
# 文件系统支持该原子操作，避免清库后才退化成单进程熔断。
CIRCUIT_PROBE="${VOC_LLM_CIRCUIT_FILE}.probe.$$"
CIRCUIT_PROBE_LINK="${VOC_LLM_CIRCUIT_FILE}.probe-link.$$"
if ! (umask 077 && : > "$CIRCUIT_PROBE" && ln "$CIRCUIT_PROBE" "$CIRCUIT_PROBE_LINK"); then
  rm -f -- "$CIRCUIT_PROBE" "$CIRCUIT_PROBE_LINK"
  echo "!! 共享熔断路径不支持原子发布，未做任何变更：$VOC_LLM_CIRCUIT_FILE" >&2
  exit 2
fi
rm -f -- "$CIRCUIT_PROBE" "$CIRCUIT_PROBE_LINK"
export VOC_LLM_CIRCUIT_FILE

PID_EXISTING=""
PID_INNOVATION=""
PID_FINALIZE=""
CHILDREN_DRAINED=1

terminate_and_drain() {
  local pid="$1"
  local result_var="$2"
  local finished_pid=""
  local rc=0

  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
  fi
  # Python 收到 TERM 后会发布协作取消、停止重试，并尽量等当前在飞 I/O
  # 返回后写失败 run log。70 秒覆盖正常的 60 秒 HTTP 上限；若底层 I/O
  # 仍不响应而被 KILL，该 lifecycle 日志可能缺失，整个共享 run 必须判不完整。
  wait_with_timeout "$pid" 70 finished_pid rc
  if [[ -z "$finished_pid" ]]; then
    echo "!! pid=$pid 在 70 秒内未回应 TERM，发送 KILL。"
    kill -KILL "$pid" 2>/dev/null || true
    if wait "$pid"; then
      rc=0
    else
      rc=$?
    fi
  fi
  printf -v "$result_var" '%s' "$rc"
}

wait_publisher_and_drain() {
  local pid="$1"
  local result_var="$2"
  local finished_pid=""
  local rc=0

  # 该进程是共享 fatal 的首因发布者，不向它发送 TERM，以免打断
  # shutdown(wait=True) 后的稳定账本；给同样的 70 秒自然退出窗口。
  wait_with_timeout "$pid" 70 finished_pid rc
  if [[ -z "$finished_pid" ]]; then
    echo "!! fatal 发布者 pid=$pid 在 70 秒内未退出，发送 KILL。"
    kill -KILL "$pid" 2>/dev/null || true
    if wait "$pid"; then
      rc=0
    else
      rc=$?
    fi
  fi
  printf -v "$result_var" '%s' "$rc"
}

stop_and_drain_children() {
  local pids=()
  local candidate pid seen i running

  for candidate in "$PID_EXISTING" "$PID_INNOVATION" "$PID_FINALIZE"; do
    [[ -n "$candidate" ]] || continue
    seen=0
    for pid in "${pids[@]}"; do
      if [[ "$pid" == "$candidate" ]]; then
        seen=1
        break
      fi
    done
    (( seen == 1 )) || pids+=("$candidate")
  done
  while IFS= read -r candidate; do
    [[ -n "$candidate" ]] || continue
    seen=0
    for pid in "${pids[@]}"; do
      if [[ "$pid" == "$candidate" ]]; then
        seen=1
        break
      fi
    done
    (( seen == 1 )) || pids+=("$candidate")
  done < <(jobs -pr)

  for pid in "${pids[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for ((i = 1; i <= 350; i++)); do
    running=0
    for pid in "${pids[@]}"; do
      if kill -0 "$pid" 2>/dev/null; then
        running=1
        break
      fi
    done
    if (( running == 0 )); then
      break
    fi
    sleep 0.2
  done
  for pid in "${pids[@]}"; do
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  for pid in "${pids[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
  PID_EXISTING=""
  PID_INNOVATION=""
  PID_FINALIZE=""
  CHILDREN_DRAINED=1
}

handle_signal() {
  local signal="$1"
  local rc=1
  trap '' INT TERM
  if [[ "$signal" == "INT" ]]; then
    rc=130
  elif [[ "$signal" == "TERM" ]]; then
    rc=143
  fi
  echo "!! 收到 $signal，终止并回收所有后台生成进程。"
  stop_and_drain_children
  exit "$rc"
}

handle_exit() {
  local rc=$?
  trap - EXIT
  trap '' INT TERM
  if (( CHILDREN_DRAINED == 0 )); then
    stop_and_drain_children
  fi
  exit "$rc"
}

trap 'handle_signal INT' INT
trap 'handle_signal TERM' TERM
trap handle_exit EXIT

# ---- 开跑前先把量级摆出来 ----
# 2026-08-17 教训：连续五次全量重启把百炼余额跑干，而这把 Key 与智能客服
# 项目共用，欠费同时打挂了两个项目。运维文档 §3.1 早就写了「跑大批量前先查
# 余额」，但没有任何地方把预计消耗算给人看，于是每次都是跑到断才知道。
# 这里做两件事：① 连通性/欠费前置探针，已欠费就别清库；② 按池子规模估算消耗。
echo "== 0. 前置检查：额度与规模 =="
PREFLIGHT=$(.venv/bin/python - <<'PYEOF'
import sys
sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import db, llm
try:
    llm.embed(["preflight"])
except Exception as error:
    print(f"FAIL|{type(error).__name__}: {str(error)[:160]}")
    raise SystemExit(0)
rows = db.generation_pool(None, None)
# 实测基准：单周探针 920 次调用 / 1.05M tokens，池约 900 条证据
# => 每条证据约 1,170 tokens。非线性（桶会合并），给区间。
low = int(len(rows) * 900 / 1000)
high = int(len(rows) * 1400 / 1000)
print(f"OK|{len(rows)}|{low}|{high}")
PYEOF
)
if [[ "$PREFLIGHT" == FAIL* ]]; then
  echo "!! 前置探针失败，不清库、不启动：${PREFLIGHT#FAIL|}" >&2
  exit 7
fi
IFS='|' read -r _ POOL_ROWS EST_LOW EST_HIGH <<< "$PREFLIGHT"
echo "   生成池 ${POOL_ROWS} 条证据；预计消耗约 ${EST_LOW}k–${EST_HIGH}k tokens"
echo "   （这把 Key 与智能客服项目共用，余额不足会同时打挂两个项目）"

echo "== 停止旧进程 =="
RC_STOP=0
pkill -f run_generate.py 2>/dev/null || RC_STOP=$?
if (( RC_STOP > 1 )); then
  echo "!! 停止旧进程失败，pkill 退出码 $RC_STOP。"
  exit "$RC_STOP"
fi
for ((i = 1; i <= 15; i++)); do
  pgrep -f run_generate.py >/dev/null || break
  sleep 1
done
RC_KILL=0
pkill -9 -f run_generate.py 2>/dev/null || RC_KILL=$?
if (( RC_KILL > 1 )); then
  echo "!! 强制停止旧进程失败，pkill 退出码 $RC_KILL。"
  exit "$RC_KILL"
fi
sleep 1
REMAINING="$( (pgrep -f run_generate.py || true) | wc -l | tr -d ' ')"
echo "   剩余进程: $REMAINING"
if (( REMAINING != 0 )); then
  echo "!! 仍有 run_generate.py 进程未退出，不继续清库。"
  exit 1
fi

echo "== 清空机会点层（保留事实层）=="
# 人工决策表不在清理范围内：它没有 ON DELETE CASCADE 是有意的保护。
# 库里若已有人工决策，DELETE 会因外键失败。整段清理必须同时
# 回滚，不能先提交 TRUNCATE 留下半清库。
docker exec voc-postgres psql -U voc_admin -d voc -q \
  -v ON_ERROR_STOP=1 \
  --single-transaction \
  -c "TRUNCATE voc_opp_evidence, voc_opp_snapshot, voc_proposal, voc_opp_lineage CASCADE;" \
  -c "DELETE FROM voc_opportunity;" \
  -c "DELETE FROM voc_unclassified_evidence;" || {
    echo "!! 清库失败（可能存在人工决策行）；本次清理已整体回滚。"
    exit 1
  }
OPP_COUNT="$(docker exec voc-postgres psql -U voc_admin -d voc -tAc \
  'SELECT count(*) FROM voc_opportunity')"
echo "   机会点: $OPP_COUNT"

export VOC_LLM_CONCURRENCY="$PER_PROC_CONCURRENCY"

echo "== 阶段一：两个生命周期并行生成（每进程并发 ${PER_PROC_CONCURRENCY}，run_id=${RUN_ID}）=="
CHILDREN_DRAINED=0
nohup .venv/bin/python -u scripts/run_generate.py --week 2026-W33 --full-history \
      --lifecycle existing --run-id "$RUN_ID" --limit-buckets "${BUCKETS:-0}" \
      --skip-finalize > /tmp/gen_existing.log 2>&1 &
PID_EXISTING=$!
nohup .venv/bin/python -u scripts/run_generate.py --week 2026-W33 --full-history \
      --lifecycle innovation --run-id "$RUN_ID" --limit-buckets "${BUCKETS:-0}" \
      --skip-finalize > /tmp/gen_innovation.log 2>&1 &
PID_INNOVATION=$!
echo "   老品迭代 pid=$PID_EXISTING  新品创新 pid=$PID_INNOVATION"

RC_EXISTING=-1
RC_INNOVATION=-1
FIRST_RC=0
FIRST_PID=""
FAIL_RC=0
if wait -n -p FIRST_PID "$PID_EXISTING" "$PID_INNOVATION"; then
  FIRST_RC=0
else
  FIRST_RC=$?
fi
FATAL_PUBLISHER_PID=""
if [[ -s "$VOC_LLM_CIRCUIT_FILE" ]]; then
  IFS= read -r FATAL_PUBLISHER_LINE < "$VOC_LLM_CIRCUIT_FILE" || true
  if [[ "$FATAL_PUBLISHER_LINE" =~ ^pid=([0-9]+)$ ]]; then
    FATAL_PUBLISHER_PID="${BASH_REMATCH[1]}"
  fi
fi

if [[ "$FIRST_PID" == "$PID_EXISTING" ]]; then
  RC_EXISTING=$FIRST_RC
  PID_EXISTING=""
  if (( FIRST_RC != 0 )); then
    FAIL_RC=$FIRST_RC
    if [[ "$FATAL_PUBLISHER_PID" == "$PID_INNOVATION" ]]; then
      echo "!! 老品迭代先退出但新品创新是 fatal 首因发布者；等待其稳定账本。"
      wait_publisher_and_drain "$PID_INNOVATION" RC_INNOVATION
    else
      echo "!! 老品迭代首先失败（退出码 $FIRST_RC），立即终止新品创新。"
      terminate_and_drain "$PID_INNOVATION" RC_INNOVATION
    fi
  elif wait "$PID_INNOVATION"; then
    RC_INNOVATION=0
  else
    RC_INNOVATION=$?
    FAIL_RC=$RC_INNOVATION
  fi
  PID_INNOVATION=""
else
  RC_INNOVATION=$FIRST_RC
  PID_INNOVATION=""
  if (( FIRST_RC != 0 )); then
    FAIL_RC=$FIRST_RC
    if [[ "$FATAL_PUBLISHER_PID" == "$PID_EXISTING" ]]; then
      echo "!! 新品创新先退出但老品迭代是 fatal 首因发布者；等待其稳定账本。"
      wait_publisher_and_drain "$PID_EXISTING" RC_EXISTING
    else
      echo "!! 新品创新首先失败（退出码 $FIRST_RC），立即终止老品迭代。"
      terminate_and_drain "$PID_EXISTING" RC_EXISTING
    fi
  elif wait "$PID_EXISTING"; then
    RC_EXISTING=0
  else
    RC_EXISTING=$?
    FAIL_RC=$RC_EXISTING
  fi
  PID_EXISTING=""
fi
CHILDREN_DRAINED=1

echo "   老品迭代退出码 $RC_EXISTING / 新品创新退出码 $RC_INNOVATION"
if (( RC_EXISTING != 0 || RC_INNOVATION != 0 )); then
  (( FAIL_RC != 0 )) || FAIL_RC=1
  echo "!! 至少一个生命周期生成失败，不执行统一收尾。"
  exit "$FAIL_RC"
fi

echo "== 阶段二：统一收尾（汇聚 + 拆分检测 + 快照 + 放行）=="
# 收尾单进程，可以用满整个并发额度
export VOC_LLM_CONCURRENCY=64
RC_FINALIZE=0
CHILDREN_DRAINED=0
# 收尾不取生成池，故不带 --full-history。
.venv/bin/python -u scripts/run_generate.py --week 2026-W33 \
      --lifecycle both --run-id "$RUN_ID" --finalize-only \
      > /tmp/gen_finalize.log 2>&1 &
PID_FINALIZE=$!
if wait "$PID_FINALIZE"; then
  RC_FINALIZE=0
else
  RC_FINALIZE=$?
fi
PID_FINALIZE=""
CHILDREN_DRAINED=1
echo "   收尾退出码 $RC_FINALIZE"
tail -6 /tmp/gen_finalize.log || true
(( RC_FINALIZE == 0 )) || exit "$RC_FINALIZE"
