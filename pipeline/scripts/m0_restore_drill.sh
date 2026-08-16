#!/usr/bin/env bash
# M0 验收：真实恢复演练（PRD v8 §11.2 RTO ≤4h / §12 M0 交付物）
# 用法：bash /home/sdy/voc-analytics/scripts/m0_restore_drill.sh
# 前置条件：在 ulanzicloud 上执行；voc-postgres 与源库 voc 正常；兼容的异地备份已
#   预先推送到 /tmp/drill.dump.gz；调用者可运行 docker，且宿主机有 gzip/gunzip。
# 脚本会先删除同名 voc_drill、恢复后再次销毁该临时库，并删除 /tmp/drill.dump.gz。
set -euo pipefail
CONTAINER=voc-postgres
REMOTE_HOST="${VOC_BACKUP_HOST:-opencloud}"
REMOTE_DIR="${VOC_BACKUP_DIR:-/opt/backups/voc-offsite}"
DRILL_DB=voc_drill
T0=$(date +%s)

echo "== 1. 校验【异地】副本（由 opencloud 预先推送到 /tmp/drill.dump.gz）=="
# 注：ulanzicloud 出站 SSH 因既有 ssh_config 故障不可用，改由 opencloud 推送。
[ -s /tmp/drill.dump.gz ] || { echo "   !! /tmp/drill.dump.gz 不存在，请先由 opencloud 推送" >&2; exit 2; }
gzip -t /tmp/drill.dump.gz && echo "   异地副本完整性校验 OK  $(du -h /tmp/drill.dump.gz | cut -f1)"

echo "== 2. 建临时库并恢复 =="
docker exec "$CONTAINER" psql -U voc_admin -d postgres -q \
  -c "DROP DATABASE IF EXISTS ${DRILL_DB};" -c "CREATE DATABASE ${DRILL_DB};"
gunzip -c /tmp/drill.dump.gz \
  | docker exec -i "$CONTAINER" pg_restore -U voc_admin -d "$DRILL_DB" --no-owner --no-acl 2>&1 \
  | grep -viE 'warning|already exists' | head -5 || true

echo "== 3. 结构比对 =="
q() { docker exec "$CONTAINER" psql -U voc_admin -d "$1" -tAc "$2" | tr -d ' '; }
for obj in \
  "表:SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE'" \
  "视图:SELECT count(*) FROM information_schema.views WHERE table_schema='public'" \
  "触发器:SELECT count(*) FROM information_schema.triggers WHERE trigger_schema='public'" \
  "索引:SELECT count(*) FROM pg_indexes WHERE schemaname='public'" ; do
  name="${obj%%:*}"; sql="${obj#*:}"
  a=$(q voc "$sql"); b=$(q "$DRILL_DB" "$sql")
  if [ "$a" = "$b" ]; then echo "   PASS  $name 数量一致 ($a)"; else echo "   FAIL  $name 源=$a 恢复=$b"; exit 1; fi
done

echo "== 4. 触发器功能验证（恢复库上重跑锁保护）=="
docker exec -i "$CONTAINER" psql -U voc_admin -d "$DRILL_DB" -q -v ON_ERROR_STOP=1 <<'SQL' 2>&1 | tail -3
INSERT INTO voc_opportunity(opp_id,opp_type,src_line,core_tag,title)
VALUES ('DRILL-1','老品迭代','线A','耐用性','原标题');
INSERT INTO voc_opportunity_manual(opp_id,status,updated_by) VALUES ('DRILL-1','项目中','drill');
UPDATE voc_opportunity SET title='应被拦截' WHERE opp_id='DRILL-1';
DO $$ BEGIN
  IF (SELECT title FROM voc_opportunity WHERE opp_id='DRILL-1') <> '原标题'
  THEN RAISE EXCEPTION '恢复库的锁保护触发器失效'; END IF;
  RAISE NOTICE '   PASS  恢复库触发器功能正常';
END $$;
SQL

echo "== 5. 数据行数比对 =="
for t in voc_message voc_evidence voc_opportunity voc_opp_evidence voc_opportunity_manual; do
  a=$(q voc "SELECT count(*) FROM $t"); b=$(q "$DRILL_DB" "SELECT count(*) FROM $t")
  [ "$t" = voc_opportunity ] && b=$((b-1))   # 扣除演练插入的 DRILL-1
  [ "$t" = voc_opportunity_manual ] && b=$((b-1))
  if [ "$a" = "$b" ]; then echo "   PASS  $t = $a"; else echo "   FAIL  $t 源=$a 恢复=$b"; exit 1; fi
done

echo "== 6. 销毁演练库 =="
docker exec "$CONTAINER" psql -U voc_admin -d postgres -q -c "DROP DATABASE ${DRILL_DB};"
rm -f /tmp/drill.dump.gz
echo
echo "恢复演练通过，耗时 $(( $(date +%s) - T0 )) 秒（RTO 目标 ≤4h）"
