#!/usr/bin/env bash
# M5：部署独立 Dagster 实例（PRD v8 §8.2）
# 用法：cd /home/sdy/voc-analytics && bash scripts/m5_deploy_dagster.sh
# 前置条件：在 ulanzicloud 上以能 sudo 的用户执行；源码和 .env 位于
#   /home/sdy/voc-analytics；/tmp/voc_pg.env 含管理员口令；voc_dagster 已创建；
#   Python 3.11+、/opt/uv-bootstrap/bin/uv、rsync、systemd 与 curl 可用。
# 本脚本会以 rsync --delete 同步部署目录、重建其 .venv，并覆盖对应 systemd 配置；
# 对 kol-dashboard 零侵入。
set -euo pipefail

APP=/opt/ulanzi/voc-analytics/current
DHOME=/opt/dagster/voc-analytics-production
ETC=/etc/voc-analytics
SRC=/home/sdy/voc-analytics

echo "== 1. 系统用户与目录 =="
id voc-analytics >/dev/null 2>&1 || sudo useradd -r -m -d /var/lib/voc-analytics -s /usr/sbin/nologin voc-analytics
sudo mkdir -p "$APP" "$DHOME/storage" "$ETC" /opt/ulanzi/voc-analytics
sudo rsync -a --delete --exclude .venv --exclude __pycache__ "$SRC/" "$APP/"
# 必须用【全局可读】的 Python 建 venv。
# 早期用 `uv venv --python 3.11` 会选中 root 的 uv 托管 Python（/root/.local，0700），
# venv 里的 python 变成指向 /root/... 的符号链接，voc-analytics 无法穿透 /root，
# systemd 报 203/EXEC Permission denied。
SYSPY=/opt/uv/bin/python3.11
[ -x "$SYSPY" ] || SYSPY=$(command -v python3.11 || command -v python3)
sudo rm -rf "$APP/.venv"
cd "$APP" && sudo /opt/uv-bootstrap/bin/uv venv --python "$SYSPY" .venv >/dev/null 2>&1 || true
sudo /opt/uv-bootstrap/bin/uv pip install --python "$APP/.venv/bin/python" \
  "psycopg[binary]>=3.1" "openpyxl>=3.1" "dagster>=1.7" "dagster-webserver>=1.7" \
  "dagster-postgres>=0.23" 2>&1 | tail -2

echo "== 2. runtime.env（0600, 属主 voc-analytics）=="
sudo cp "$SRC/.env" "$ETC/runtime.env"
# 追加 Dagster 元数据库连接（复用 voc_admin，库为 voc_dagster）
grep -q VOC_DAGSTER_PG_DB "$ETC/runtime.env" 2>/dev/null || sudo tee -a "$ETC/runtime.env" >/dev/null <<EOF
VOC_DAGSTER_PG_USER=voc_admin
VOC_DAGSTER_PG_PASSWORD=$(sudo grep '^VOC_ADMIN_PASSWORD' /tmp/voc_pg.env | cut -d= -f2)
VOC_DAGSTER_PG_HOST=127.0.0.1
VOC_DAGSTER_PG_PORT=5434
VOC_DAGSTER_PG_DB=voc_dagster
DAGSTER_HOME=$DHOME
EOF
sudo chown voc-analytics:voc-analytics "$ETC/runtime.env"
sudo chmod 600 "$ETC/runtime.env"

echo "== 3. dagster.yaml / workspace.yaml =="
sudo tee "$DHOME/dagster.yaml" >/dev/null <<'EOF'
storage:
  postgres:
    postgres_db:
      username: { env: VOC_DAGSTER_PG_USER }
      password: { env: VOC_DAGSTER_PG_PASSWORD }
      hostname: { env: VOC_DAGSTER_PG_HOST }
      db_name:  { env: VOC_DAGSTER_PG_DB }
      port:     { env: VOC_DAGSTER_PG_PORT }
run_coordinator:
  module: dagster.core.run_coordinator
  class: QueuedRunCoordinator
  config:
    max_concurrent_runs: 2
    tag_concurrency_limits:
      - key: voc/llm
        limit: 1
      - key: voc/external-writer
        limit: 1
        value: { applyLimitPerUniqueValue: true }
run_launcher: { module: dagster, class: DefaultRunLauncher }
compute_logs:
  module: dagster.core.storage.local_compute_log_manager
  class: LocalComputeLogManager
  config: { base_dir: /opt/dagster/voc-analytics-production/storage }
telemetry: { enabled: false }
retention:
  schedule: { purge_after_days: 90 }
  sensor:   { purge_after_days: 30 }
EOF
sudo tee "$ETC/workspace.production.yaml" >/dev/null <<'EOF'
load_from:
  - grpc_server:
      host: 127.0.0.1
      port: 4003
      location_name: voc-analytics
EOF
sudo chown -R voc-analytics:voc-analytics "$DHOME" "$APP"

echo "== 4. 三个 systemd 单元 =="
common_unit() { cat <<EOF
[Unit]
Description=$1
After=network-online.target
Wants=network-online.target
$2

[Service]
Type=simple
User=voc-analytics
Group=voc-analytics
WorkingDirectory=$APP
EnvironmentFile=$ETC/runtime.env
Environment=DAGSTER_HOME=$DHOME
UMask=0077
ExecStart=$3
Restart=on-failure
RestartSec=5s
StartLimitIntervalSec=0
TimeoutStopSec=90s
KillSignal=SIGTERM
MemoryMax=1G
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=$DHOME
ProtectHome=read-only
ProtectKernelTunables=true
ProtectKernelModules=true

[Install]
WantedBy=multi-user.target
EOF
}
common_unit "VOC Analytics Dagster gRPC code location" "" \
  "$APP/.venv/bin/dagster code-server start --host 127.0.0.1 --port 4003 --location-name voc-analytics --module-name voc_analytics.definitions --use-python-environment-entry-point" \
  | sudo tee /etc/systemd/system/voc-analytics-grpc.service >/dev/null
common_unit "VOC Analytics Dagster Daemon" \
  "Requires=voc-analytics-grpc.service
After=voc-analytics-grpc.service" \
  "$APP/.venv/bin/python -m dagster._daemon run -w $ETC/workspace.production.yaml" \
  | sudo tee /etc/systemd/system/voc-analytics-dagster-daemon.service >/dev/null
common_unit "VOC Analytics Dagster Webserver" \
  "Requires=voc-analytics-grpc.service
After=voc-analytics-grpc.service" \
  "$APP/.venv/bin/python -m dagster_webserver -h 127.0.0.1 -p 3002 -w $ETC/workspace.production.yaml" \
  | sudo tee /etc/systemd/system/voc-analytics-dagster-webserver.service >/dev/null

echo "== 5. 启动 =="
sudo systemctl daemon-reload
sudo systemctl enable --now voc-analytics-grpc.service
sleep 5
sudo systemctl enable --now voc-analytics-dagster-daemon.service voc-analytics-dagster-webserver.service
sleep 5

echo "== 6. 验收 =="
for s in voc-analytics-grpc voc-analytics-dagster-daemon voc-analytics-dagster-webserver; do
  st=$(systemctl is-active "$s")
  echo "   $s: $st"
  [ "$st" = active ] || { sudo journalctl -u "$s" -n 10 --no-pager; exit 1; }
done
curl -s -o /dev/null -w "   webserver http: %{http_code}\n" http://127.0.0.1:3002/server_info
echo "Dagster 独立实例部署完成（webserver 127.0.0.1:3002，经 Tailscale serve 暴露可选）"
