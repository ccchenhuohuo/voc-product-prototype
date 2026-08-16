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
docker exec "$CONTAINER" psql -U voc_admin -d voc -tAc \
  "SELECT name FROM pg_available_extensions WHERE name IN ('vector','pg_bigm','pgroonga')"

echo "== 6. 执行 DDL =="
for f in 001_schema 002_triggers 003_views 004_roles; do
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
echo "M0 建库完成"
