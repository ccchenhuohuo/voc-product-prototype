#!/usr/bin/env bash
# 清库 + 两条线并行重跑 + 统一收尾。
# 用法：bash ~/voc-analytics/scripts/rerun_both.sh
# 前置条件：~/voc-analytics 下已有 .env、.venv 与事实层数据；voc-postgres 正常；
#   百炼/LLM 凭据与配额可用。目标周当前固定为 2026-W33，可用 BUCKETS 和
#   PER_PROC_CONCURRENCY 调整规模/并发。
# 警告：脚本会终止匹配 run_generate.py 的进程并清空机会点层；若存在受外键保护的
# 人工决策则按设计中止。执行前应确认备份并排除其他生成任务。
#
# 两段式不是为了好看，是正确性要求：跨线汇聚要读【对侧已落库的机会点】，
# 两条线并行时先收工的那条看到的对侧是残缺的（实测线A 13:00 完、线B 13:41 完，
# 线A 的汇聚等于对着半个库跑）。所以并行只到生成为止，汇聚/拆分/快照/放行
# 等两条线都结束后统一做一遍。
set -uo pipefail
cd ~/voc-analytics

# 全局闸门 _GATE 是【进程内】的信号量，两个进程各持一份。实测拐点是合计 64，
# 128 会触发 429 Throttling，所以每个进程分一半。
PER_PROC_CONCURRENCY="${PER_PROC_CONCURRENCY:-32}"

echo "== 停止旧进程 =="
pkill -f run_generate.py 2>/dev/null
for i in $(seq 1 15); do
  pgrep -f run_generate.py >/dev/null || break
  sleep 1
done
pkill -9 -f run_generate.py 2>/dev/null
sleep 1
echo "   剩余进程: $(pgrep -f run_generate.py | wc -l)"

echo "== 清空机会点层（保留事实层）=="
# 人工决策表不在清理范围内：它没有 ON DELETE CASCADE 是有意的保护。
# 库里若已有人工决策，下面这句会因外键失败——那是应有的行为，不要绕过。
docker exec voc-postgres psql -U voc_admin -d voc -q \
  -c "TRUNCATE voc_opp_evidence, voc_opp_snapshot, voc_proposal, voc_opp_lineage CASCADE;" \
  -c "DELETE FROM voc_opportunity;" \
  -c "DELETE FROM voc_unclassified_evidence;" || {
    echo "!! 清库失败（可能存在人工决策行）。中止。"; exit 1; }
echo "   机会点: $(docker exec voc-postgres psql -U voc_admin -d voc -tAc 'SELECT count(*) FROM voc_opportunity')"

set -a; . ./.env; set +a
export VOC_LLM_CONCURRENCY="$PER_PROC_CONCURRENCY"

echo "== 阶段一：两条线并行生成（每进程并发 ${PER_PROC_CONCURRENCY}）=="
nohup .venv/bin/python -u scripts/run_generate.py --week 2026-W33 --line A \
      --limit-buckets "${BUCKETS:-12}" --skip-cross > /tmp/gen_a.log 2>&1 &
PID_A=$!
nohup .venv/bin/python -u scripts/run_generate.py --week 2026-W33 --line B \
      --skip-cross > /tmp/gen_b.log 2>&1 &
PID_B=$!
echo "   线A pid=$PID_A  线B pid=$PID_B"

wait $PID_A; RC_A=$?
wait $PID_B; RC_B=$?
echo "   线A 退出码 $RC_A / 线B 退出码 $RC_B"

echo "== 阶段二：统一收尾（跨线汇聚 + 拆分检测 + 快照 + 放行）=="
# 收尾单进程，可以用满整个并发额度
export VOC_LLM_CONCURRENCY=64
.venv/bin/python -u scripts/run_generate.py --week 2026-W33 --cross-only \
      > /tmp/gen_cross.log 2>&1
echo "   收尾退出码 $?"
tail -6 /tmp/gen_cross.log
