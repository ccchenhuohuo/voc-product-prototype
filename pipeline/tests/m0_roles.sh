#!/usr/bin/env bash
# M0 验收：角色权限矩阵必须真的生效
# 用法：cd /home/sdy/voc-analytics && bash tests/m0_roles.sh
# 前置条件：voc-postgres 容器及当前 schema/角色已部署；调用者可执行 docker；
#   VOC_WRITER_PASSWORD、VOC_HUMAN_PASSWORD、VOC_READER_PASSWORD 已导出。
#   脚本会写入 R-TEST 验收行；应在隔离验收环境运行，勿与同名验收并发运行。
set -uo pipefail
PASS=0; FAIL=0
run() {  # run <user> <password> <sql>
  docker exec -e PGPASSWORD="$2" -i voc-postgres \
    psql -h 127.0.0.1 -U "$1" -d voc -tAc "$3" 2>&1 | head -2
}
check() { # check <desc> <expect: ok|deny> <output>
  local desc="$1" expect="$2" out="$3"
  local denied=0
  grep -qiE 'permission denied|拒绝访问' <<<"$out" && denied=1
  if { [ "$expect" = deny ] && [ $denied = 1 ]; } || { [ "$expect" = ok ] && [ $denied = 0 ] && ! grep -qi '^ERROR' <<<"$out"; }; then
    echo "  PASS  $desc"; PASS=$((PASS+1))
  else
    echo "  FAIL  $desc"; echo "        -> $(head -1 <<<"$out")"; FAIL=$((FAIL+1))
  fi
}

docker exec -i voc-postgres psql -U voc_admin -d voc -q \
  -c "DELETE FROM voc_opportunity WHERE opp_id='R-TEST';" >/dev/null 2>&1

check "voc_writer 可写机会点表" ok "$(run voc_writer "$VOC_WRITER_PASSWORD" \
  "INSERT INTO voc_opportunity(opp_id,opp_type,src_line,core_tag,title) VALUES ('R-TEST','老品迭代','线A','t','t');")"
check "voc_writer 不可写人工表" deny "$(run voc_writer "$VOC_WRITER_PASSWORD" \
  "INSERT INTO voc_opportunity_manual(opp_id,status,updated_by) VALUES ('R-TEST','项目中','w');")"
check "voc_writer 可读人工表" ok "$(run voc_writer "$VOC_WRITER_PASSWORD" \
  "SELECT count(*) FROM voc_opportunity_manual;")"
check "voc_human 可写人工表" ok "$(run voc_human "$VOC_HUMAN_PASSWORD" \
  "INSERT INTO voc_opportunity_manual(opp_id,status,updated_by) VALUES ('R-TEST','项目中','pm');")"
check "voc_human 不可改机会点表" deny "$(run voc_human "$VOC_HUMAN_PASSWORD" \
  "UPDATE voc_opportunity SET title='篡改' WHERE opp_id='R-TEST';")"
check "voc_human 可裁决提案状态" ok "$(run voc_human "$VOC_HUMAN_PASSWORD" \
  "SELECT count(*) FROM voc_proposal;")"
check "voc_reader 可读 voc_board" ok "$(run voc_reader "$VOC_READER_PASSWORD" \
  "SELECT count(*) FROM voc_board;")"
check "voc_reader 不可写人工表" deny "$(run voc_reader "$VOC_READER_PASSWORD" \
  "UPDATE voc_opportunity_manual SET owner='x' WHERE opp_id='R-TEST';")"
check "voc_reader 不可写机会点表" deny "$(run voc_reader "$VOC_READER_PASSWORD" \
  "UPDATE voc_opportunity SET title='x' WHERE opp_id='R-TEST';")"
check "voc_reader 可下钻证据" ok "$(run voc_reader "$VOC_READER_PASSWORD" \
  "SELECT count(*) FROM voc_opp_evidence;")"

docker exec -i voc-postgres psql -U voc_admin -d voc -q \
  -c "DELETE FROM voc_opportunity WHERE opp_id='R-TEST';" >/dev/null 2>&1

echo
echo "角色权限验收: PASS=$PASS FAIL=$FAIL"
[ "$FAIL" -eq 0 ]
