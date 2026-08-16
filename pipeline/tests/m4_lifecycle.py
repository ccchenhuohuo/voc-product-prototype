#!/usr/bin/env python3
"""M4 验收：生命周期状态机的应用层逻辑（PRD v8 §7）。

数据库层（触发器）已在 M0 验证。本脚本验证应用层三件事：
  · 人工状态锁与墓碑基准读取
  · 墓碑唤醒：累计达基准 3 倍提 REVIVE；被拒后基准重置
  · 陈旧检测 + 合并方向：僵尸条目不得反向吃掉新条目

专用墓碑抑制函数 check_tombstone() 当前未接入生成流程，本脚本也不验证该能力。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python tests/m4_lifecycle.py

前置条件：
  Python 3.11+ 及项目依赖已安装；数据库已完成当前迁移，writer/human 连接与触发器
  均可用。本脚本会写入并清理 LC-* 测试数据，勿与其他生命周期验收并发运行。
"""
from __future__ import annotations
import sys
sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import db, lifecycle  # noqa: E402

P = F = 0


def chk(name: str, ok: bool, detail: str = "") -> None:
    global P, F
    if ok:
        P += 1; print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        F += 1; print(f"  FAIL  {name}  {detail}")


def cleanup() -> None:
    # 顺序与角色都不能错：人工表由 voc_human 清，且必须先于机器表——
    # voc_opportunity_manual 对 voc_opportunity 的外键【没有】ON DELETE CASCADE，
    # 这是有意的保护（删机器行不得静默抹掉人工决策）。
    db.execute_as_human("DELETE FROM voc_status_log WHERE opp_id LIKE 'LC-%'")
    db.execute_as_human("DELETE FROM voc_opportunity_manual WHERE opp_id LIKE 'LC-%'")
    db.execute("DELETE FROM voc_proposal WHERE opp_ids && ARRAY['LC-TOMB','LC-STALE','LC-NEW']")
    db.execute("DELETE FROM voc_opp_snapshot WHERE opp_id LIKE 'LC-%'")
    db.execute("DELETE FROM voc_opportunity WHERE opp_id LIKE 'LC-%'")


cleanup()
print("=== M4 生命周期状态机验收 ===\n")

# ---------- 准备：一个墓碑条目 ----------
db.execute("""INSERT INTO voc_opportunity(opp_id,opp_type,src_line,core_tag,title,
                problem_mode,evi_total,first_week,last_week)
              VALUES('LC-TOMB','老品迭代','电商','耐用性','测试墓碑：修复断裂',
                     '铰链在两个月内断裂',5,'2026-W30','2026-W30')""")
db.execute("""INSERT INTO voc_opp_snapshot(opp_id,week,evi_total)
              VALUES('LC-TOMB','2026-W30',5)""")
db.execute_as_human("""INSERT INTO voc_opportunity_manual(opp_id,status,decision_note,updated_by)
              VALUES('LC-TOMB','不考虑','成本过高，本季不做','tester')""")

# ---------- 1. 状态与锁 ----------
chk("墓碑状态正确", lifecycle.status_of("LC-TOMB") == "不考虑")
chk("墓碑处于 locked", lifecycle.is_locked("LC-TOMB"))
chk("「不考虑」写入了审计日志",
    (db.q1("SELECT count(*) FROM voc_status_log WHERE opp_id='LC-TOMB'") or 0) >= 1)

# ---------- 2. 墓碑基准 ----------
base = lifecycle.tombstone_baseline("LC-TOMB")
chk("墓碑基准取自置为「不考虑」当周的快照", base == 5, f"基准={base}")

# ---------- 3. 唤醒：未达 3 倍不提案 ----------
n = lifecycle.check_revive("2026-W33")
chk("未达 3 倍基准时不提 REVIVE", n == 0, f"提案数={n}")

# ---------- 4. 唤醒：达 3 倍则提案 ----------
db.execute("UPDATE voc_opportunity SET evi_total=15 WHERE opp_id='LC-TOMB'")
n = lifecycle.check_revive("2026-W33")
chk("达到 3 倍基准时提 REVIVE 提案", n == 1, f"提案数={n}")
pend = db.q("""SELECT rationale, payload FROM voc_proposal
                WHERE op_type='REVIVE' AND 'LC-TOMB'=ANY(opp_ids) AND status='pending'""")
chk("REVIVE 提案落在 voc_proposal（非 lineage）", len(pend) == 1)

# ---------- 5. 重复提案抑制 ----------
n2 = lifecycle.check_revive("2026-W33")
chk("已有 pending 提案时不重复提", n2 == 0, f"再次提案数={n2}")

# ---------- 6. 被拒后基准重置 ----------
db.execute_as_human("""UPDATE voc_proposal SET status='rejected', decided_by='pm', decided_at=now()
               WHERE op_type='REVIVE' AND 'LC-TOMB'=ANY(opp_ids)""")
n3 = lifecycle.check_revive("2026-W33")
chk("被拒后不再用旧基准重复提案（基准已重置）", n3 == 0, f"提案数={n3}")
db.execute("UPDATE voc_opportunity SET evi_total=50 WHERE opp_id='LC-TOMB'")
n4 = lifecycle.check_revive("2026-W33")
chk("证据再涨到新基准 3 倍时可再次提案", n4 == 1, f"提案数={n4}")

# ---------- 7. 陈旧检测 ----------
db.execute("""INSERT INTO voc_opportunity(opp_id,opp_type,src_line,core_tag,title,problem_mode)
              VALUES('LC-STALE','老品迭代','电商','磁吸','测试僵尸：强化磁力','磁力不足')""")
db.execute_as_human("""INSERT INTO voc_opportunity_manual(opp_id,status,owner,updated_by)
              VALUES('LC-STALE','项目中','pm-x','tester')""")
db.execute_as_human("""UPDATE voc_opportunity_manual SET updated_at = now() - interval '10 weeks'
               WHERE opp_id='LC-STALE'""")
stale = [x["opp_id"] for x in lifecycle.stale_items()]
chk("超 8 周未更新的「项目中」被判为陈旧", "LC-STALE" in stale)
chk("voc_board 的 status_stale 同步生效",
    db.q1("SELECT status_stale FROM voc_board WHERE opp_id='LC-STALE'") is True)

# ---------- 8. 合并方向：僵尸条目不得反向吃掉新条目 ----------
db.execute("""INSERT INTO voc_opportunity(opp_id,opp_type,src_line,core_tag,title,problem_mode)
              VALUES('LC-NEW','老品迭代','电商','磁吸','新条目：强化磁力','磁力不足')""")
target, source, manual = lifecycle.merge_direction("LC-STALE", "LC-NEW")
chk("合并方向指向高优先级方（项目中 > 考虑中）", target == "LC-STALE",
    f"target={target}")
chk("目标方陈旧时【只提提案不自动执行】", manual is True,
    "防止半年前的僵尸条目吃掉新的、更准确的机会点")

# ---------- 9. 优先级表完整 ----------
chk("五态优先级齐全", set(lifecycle.PRIORITY) ==
    {"不考虑", "考虑中", "在跟进", "项目中", "已完成"})

cleanup()
print(f"\nM4 验收: PASS={P} FAIL={F}")
sys.exit(1 if F else 0)
