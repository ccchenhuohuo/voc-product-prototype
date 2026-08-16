#!/usr/bin/env bash
# VOC 本地备份（在 ulanzicloud 上执行；PRD v8 §11.3）
# 用法：bash /home/sdy/voc-analytics/scripts/voc_backup.sh
# 前置条件：voc-postgres 容器正常；调用者可运行 docker 和 sudo；宿主机有 gzip，
#   /opt/backups/voc 有足够空间。脚本会删除该目录中 14 天前的 voc_*.dump.gz。
#   · 全量 pg_dump，本地保留 14 天
#   · 三张【不可重建】人工表额外单独导出（机器产出可重跑，人工决策不能）
#   · 异地副本由 opencloud 主动拉取（见 voc_backup_pull.sh）——
#     采用 pull 模型有两个原因：
#       1) ulanzicloud 的 /etc/ssh/ssh_config 存在既有故障（见 README 运维备注），
#          出站 SSH 不可用；
#       2) pull 模型本身更安全：生产机无法触达或删除异地备份。
set -euo pipefail
CONTAINER=voc-postgres
LOCAL=/opt/backups/voc
STAMP=$(date +%Y%m%d_%H%M%S)
HUMAN_TABLES="voc_opportunity_manual voc_status_log voc_proposal"

sudo mkdir -p "$LOCAL"; sudo chown "$(id -u):$(id -g)" "$LOCAL"

echo "== 全量备份 =="
docker exec "$CONTAINER" pg_dump -U voc_admin -d voc -Fc \
  | gzip > "$LOCAL/voc_full_${STAMP}.dump.gz"

echo "== 人工表单独备份（不可重建数据）=="
docker exec "$CONTAINER" pg_dump -U voc_admin -d voc -Fc \
  $(for t in $HUMAN_TABLES; do printf ' -t %s' "$t"; done) \
  | gzip > "$LOCAL/voc_human_${STAMP}.dump.gz"

echo "== 校验 =="
for f in "$LOCAL/voc_full_${STAMP}.dump.gz" "$LOCAL/voc_human_${STAMP}.dump.gz"; do
  gzip -t "$f" && echo "   OK $(basename "$f") $(du -h "$f" | cut -f1)"
done

echo "== 记录最新备份标记（供 pull 端校验新鲜度）=="
echo "$STAMP" > "$LOCAL/LATEST"

echo "== 清理 14 天前 =="
find "$LOCAL" -name 'voc_*.dump.gz' -mtime +14 -print -delete
echo "本地备份完成 $STAMP"
