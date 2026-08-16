#!/usr/bin/env bash
# VOC 异地备份拉取（在 opencloud 上执行，建议每日 cron；PRD v8 §11.3）
# 用法：在 opencloud 上 bash <本脚本路径>（该机登录身份是 chenyu，不是源机的 sdy）
# 前置条件：opencloud 已配置到源机的 BatchMode 免交互 SSH；ssh、rsync、gzip 与支持
#   `date -d` 的 date 可用；目标目录可写。可用 VOC_SRC_HOST、VOC_SRC_DIR、
#   VOC_OFFSITE_DIR、VOC_MAX_AGE_H 覆盖默认值；脚本会删除目标中 14 天前的备份。
# pull 模型：备份机主动拉，生产机无权删除异地副本。
set -euo pipefail
SRC_HOST="${VOC_SRC_HOST:-sdy@ulanzicloud}"   # Tailscale 主机别名；不写 IP，仓库要推远端
SRC_DIR="${VOC_SRC_DIR:-/opt/backups/voc}"
DEST="${VOC_OFFSITE_DIR:-/opt/backups/voc-offsite}"
MAX_AGE_H="${VOC_MAX_AGE_H:-30}"          # 源端备份超过此时长即告警

mkdir -p "$DEST"

echo "== 1. 检查源端备份新鲜度 =="
LATEST=$(ssh -o ConnectTimeout=15 -o BatchMode=yes "$SRC_HOST" "cat ${SRC_DIR}/LATEST 2>/dev/null" || true)
if [ -z "$LATEST" ]; then
  echo "   !! 源端无 LATEST 标记，备份可能从未执行" >&2; exit 3
fi
SRC_EPOCH=$(date -d "${LATEST:0:8} ${LATEST:9:2}:${LATEST:11:2}:${LATEST:13:2}" +%s 2>/dev/null || echo 0)
AGE_H=$(( ( $(date +%s) - SRC_EPOCH ) / 3600 ))
echo "   源端最新备份 $LATEST（${AGE_H}h 前）"
[ "$AGE_H" -gt "$MAX_AGE_H" ] && echo "   !! 超过 ${MAX_AGE_H}h 未备份，RPO 不达标" >&2

echo "== 2. 拉取 =="
rsync -az --timeout=300 --partial \
  "${SRC_HOST}:${SRC_DIR}/voc_*.dump.gz" "${SRC_HOST}:${SRC_DIR}/LATEST" "$DEST/"

echo "== 3. 校验异地副本完整性 =="
n=0
for f in "$DEST"/voc_*.dump.gz; do
  gzip -t "$f" || { echo "   !! 损坏: $f" >&2; exit 4; }
  n=$((n+1))
done
echo "   $n 个文件校验通过，占用 $(du -sh "$DEST" | cut -f1)"

echo "== 4. 清理 14 天前 =="
find "$DEST" -name 'voc_*.dump.gz' -mtime +14 -print -delete
echo "异地拉取完成"
