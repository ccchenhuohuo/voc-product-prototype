#!/usr/bin/env bash
# M0：部署 voc-postgres 并建库建表建角色
# 用法：cd /home/sdy/voc-analytics && bash scripts/m0_deploy_pg.sh
# 前置条件：在 ulanzicloud 上以可运行 Docker、sudo 的用户执行；当前目录必须包含
#   sql/；5434 端口及 /data/db/voc-postgres 可用；VOC_ADMIN_PASSWORD、
#   VOC_WRITER_PASSWORD、VOC_HUMAN_PASSWORD、VOC_READER_PASSWORD 已导出。
# 口令从环境变量注入，不落盘到仓库。本脚本会创建容器、数据库、schema 与角色。
set -euo pipefail

CONTAINER=voc-postgres
PORT=5434
DATA=/data/db/voc-postgres
IMAGE=pgvector/pgvector:pg18
: "${VOC_ADMIN_PASSWORD:?需要设置 VOC_ADMIN_PASSWORD}"
: "${VOC_WRITER_PASSWORD:?需要设置 VOC_WRITER_PASSWORD}"
: "${VOC_HUMAN_PASSWORD:?需要设置 VOC_HUMAN_PASSWORD}"
: "${VOC_READER_PASSWORD:?需要设置 VOC_READER_PASSWORD}"

echo "== 1. 拉取镜像 =="
docker image inspect "$IMAGE" >/dev/null 2>&1 || docker pull "$IMAGE"

echo "== 2. 启动容器 =="
if docker ps -a --format '{{.Names}}' | grep -qx "$CONTAINER"; then
  echo "   容器已存在，跳过创建"
else
  sudo mkdir -p "$DATA"
  docker run -d --name "$CONTAINER" --restart unless-stopped \
    -e POSTGRES_USER=voc_admin \
    -e POSTGRES_PASSWORD="$VOC_ADMIN_PASSWORD" \
    -e POSTGRES_DB=voc \
    -e TZ=Asia/Shanghai \
    -p 127.0.0.1:${PORT}:5432 \
    -v "$DATA":/var/lib/postgresql \
    --shm-size=512m \
    "$IMAGE" \
    -c shared_buffers=512MB \
    -c max_connections=60
fi

echo "== 3. 等待就绪 =="
for i in $(seq 1 60); do
  if docker exec "$CONTAINER" pg_isready -U voc_admin -d voc >/dev/null 2>&1; then break; fi
  sleep 2
done
docker exec "$CONTAINER" pg_isready -U voc_admin -d voc

echo "== 4. 建 Dagster 元数据库 =="
docker exec "$CONTAINER" psql -U voc_admin -d voc -tAc \
  "SELECT 1 FROM pg_database WHERE datname='voc_dagster'" | grep -q 1 \
  || docker exec "$CONTAINER" psql -U voc_admin -d voc \
       -c "CREATE DATABASE voc_dagster OWNER voc_admin;"

echo "== 5. 扩展可用性检查 =="
# 查的必须是本项目实际使用的扩展。原来写的是 pg_bigm/pgroonga——那是别的项目的
# 中文分词方案，本项目用 pg_trgm，检查恒为「缺失」却没人看，等于没检查。
MISSING=$(docker exec "$CONTAINER" psql -U voc_admin -d voc -tAc \
  "SELECT string_agg(x, ',') FROM unnest(ARRAY['vector','pg_trgm']) x
    WHERE x NOT IN (SELECT name FROM pg_available_extensions)")
[ -z "$MISSING" ] || { echo "!! 缺少必需扩展: $MISSING" >&2; exit 6; }
echo "   vector / pg_trgm 均可用"

echo "== 6. 执行 DDL =="
# 只跑空库可安全执行的结构性迁移。排除的三类及原因：
#   · 011 / 012 —— 自检要插 __selftest__ 行，而人工表对 voc_opportunity 有外键，
#     空库无机会点可引用，必然失败。它们的触发器定义本身是需要的。
#   · 013 / 015 —— 断言写死 645 行存量分布，只适用于已核实的存量库升级。
# 因此本脚本**建立的不是一个可直接上线的完整库**，见收尾提示。
EMPTY_DB_SAFE="001_schema 002_triggers 003_views 004_roles 005_pm 006_execute \
               007_rebuild 008_product_fields 009_spu_layer 010_app_read_grants \
               014_spu_clean_fields 016_lifecycle_isolation"
for f in $EMPTY_DB_SAFE; do
  echo "   -> $f.sql"
  docker exec -i "$CONTAINER" psql -U voc_admin -d voc -v ON_ERROR_STOP=1 < "sql/${f}.sql"
done

echo "== 7. 设置角色口令 =="
docker exec "$CONTAINER" psql -U voc_admin -d voc -v ON_ERROR_STOP=1 \
  -c "ALTER ROLE voc_writer PASSWORD '${VOC_WRITER_PASSWORD}';" \
  -c "ALTER ROLE voc_human  PASSWORD '${VOC_HUMAN_PASSWORD}';" \
  -c "ALTER ROLE voc_reader PASSWORD '${VOC_READER_PASSWORD}';"

echo "== 8. 验收 =="
docker exec "$CONTAINER" psql -U voc_admin -d voc -tAc "
  SELECT 'tables=' || count(*) FROM information_schema.tables
   WHERE table_schema='public' AND table_type='BASE TABLE';"
docker exec "$CONTAINER" psql -U voc_admin -d voc -tAc "
  SELECT 'views=' || count(*) FROM information_schema.views WHERE table_schema='public';"
docker exec "$CONTAINER" psql -U voc_admin -d voc -tAc "
  SELECT 'triggers=' || count(*) FROM information_schema.triggers
   WHERE trigger_schema='public';"

cat <<'REMAIN'

== 9. 本脚本【未】应用的迁移，上线前必须另行处理 ==
   011_audit_after_trigger.sql       人工层 AFTER 审计（自检需已有机会点可引用）
   012_audit_after_trigger_opp.sql   机会点人工层 AFTER 审计（同上）
   013_src_line_rename.sql           仅存量库升级：断言写死 645 行，空库不适用
   015_classification_contract.sql   仅存量库升级：断言写死 645 行，空库不适用

   011/012 的触发器定义是当前最终审计行为，缺了它们人工操作不会留审计日志；
   015 的 classification_state / classify_rule 两列与看板视图口径也缺失，
   应用会因缺列而报错。灌入基础数据后请逐个受控应用并核对各自的自检。

!! 因此这个库【还不能直接上线】。要真正做到一键空库建库，需要把上述四个迁移
   拆成「结构」与「基于存量数据的校验」两部分——那是独立的一项工作，尚未做。
REMAIN
echo "M0 建库完成（结构部分）"
