#!/usr/bin/env python3
"""M4 验收：提案执行器（二期 PRD §6.3）。

一期的缺口是「PM 裁决后无人执行」——本脚本用合成数据把闭环走一遍：
提案 accepted → 执行 → 证据迁移 / 血缘写入 / 源条目标注去向 / 幂等重跑。
全部对象带 EX- 前缀，跑完清理，不碰真实数据。

用法：
  cd /home/sdy/voc-analytics
  set -a; . ./.env; set +a
  .venv/bin/python tests/m4_execute.py

前置条件：
  Python 3.11+ 及项目依赖已安装；数据库已完成当前迁移，writer/human 连接均可用，
  且 voc_evidence 至少有 3 行。本脚本会写入并清理 EX-* / 哨兵周测试数据，勿并发运行。
"""
from __future__ import annotations
import sys

sys.path.insert(0, "/home/sdy/voc-analytics")
from voc_analytics import db, execute  # noqa: E402

W = "9999-W02"
P = F = 0


def chk(name: str, ok: bool, detail: str = "") -> None:
    global P, F
    if ok:
        P += 1; print(f"  PASS  {name}" + (f"   ({detail})" if detail else ""))
    else:
        F += 1; print(f"  FAIL  {name}   {detail}")


def cleanup() -> None:
    db.execute_as_human("DELETE FROM voc_status_log WHERE opp_id LIKE 'EX-%'")
    db.execute_as_human("DELETE FROM voc_opportunity_manual WHERE opp_id LIKE 'EX-%'")
    db.execute("DELETE FROM voc_opp_lineage WHERE week=%s", [W])
    db.execute("DELETE FROM voc_proposal WHERE week=%s", [W])
    db.execute("DELETE FROM voc_opp_evidence WHERE opp_id LIKE 'EX-%'")
    db.execute("DELETE FROM voc_opportunity WHERE opp_id LIKE 'EX-%'")


cleanup()
print("=== M4 提案执行器验收 ===\n")

# 借用两条真实消息做证据载体（voc_opp_evidence 对 voc_message 有外键）
msgs = db.q("SELECT message_id, seq FROM voc_evidence ORDER BY message_id LIMIT 3")
assert len(msgs) >= 3, "事实层数据不足，无法构造证据"

for oid, title, fw in (("EX-TARGET", "目标条目：强化磁吸", "2026-W20"),
                       ("EX-SOURCE", "源条目：磁力不足", "2026-W28")):
    db.execute("""INSERT INTO voc_opportunity(opp_id,opp_type,src_line,core_tag,title,
                    problem_mode,first_week,last_week,backlog)
                  VALUES(%s,'老品迭代','线A','磁吸',%s,'磁吸模块吸附力不足',%s,%s,false)""",
               [oid, title, fw, fw])

# 目标 1 条、源 2 条，其中 1 条两侧重合（验证 ON CONFLICT 不炸且不重复计数）
db.execute("""INSERT INTO voc_opp_evidence(opp_id,message_id,seq,attach_week,match_by,confidence)
              VALUES('EX-TARGET',%s,%s,'2026-W20','rule',1.0)""",
           [msgs[0]["message_id"], msgs[0]["seq"]])
for m in (msgs[0], msgs[1], msgs[2]):
    db.execute("""INSERT INTO voc_opp_evidence(opp_id,message_id,seq,attach_week,match_by,confidence)
                  VALUES('EX-SOURCE',%s,%s,'2026-W28','rule',1.0)""",
               [m["message_id"], m["seq"]])

pid = db.q1("""INSERT INTO voc_proposal(op_type,opp_ids,rationale,week,status,decided_by,payload)
               VALUES('MERGE',ARRAY['EX-TARGET','EX-SOURCE'],'两者指向同一失效模式',%s,
                      'accepted','dev:tester','{"target":"EX-TARGET"}'::jsonb)
               RETURNING proposal_id""", [W])

# ---------- 1. 执行 ----------
stat = execute.run(W)
chk("MERGE 被执行", stat["MERGE"] == 1, str(stat))

n_t = db.q1("SELECT count(*) FROM voc_opp_evidence WHERE opp_id='EX-TARGET'")
n_s = db.q1("SELECT count(*) FROM voc_opp_evidence WHERE opp_id='EX-SOURCE'")
chk("证据全部迁到目标且重合条不重复", n_t == 3, f"目标 {n_t} 条（期望 3）")
chk("源条目证据已清空", n_s == 0, f"源 {n_s} 条")

merged_by = db.q1("SELECT count(*) FROM voc_opp_evidence "
                  "WHERE opp_id='EX-TARGET' AND match_by='merge'")
chk("迁移证据标记 match_by='merge'（可追溯来源）", merged_by == 2, f"{merged_by} 条")

# ---------- 2. 源条目处置 ----------
row = db.q("SELECT backlog, merged_into FROM voc_opportunity WHERE opp_id='EX-SOURCE'")[0]
chk("源条目移出 PM 视野", row["backlog"] is True)
chk("源条目标注并入去向", row["merged_into"] == "EX-TARGET", str(row["merged_into"]))
chk("源条目未被物理删除（保护人工决策外键）",
    db.q1("SELECT count(*) FROM voc_opportunity WHERE opp_id='EX-SOURCE'") == 1)

# ---------- 3. 血缘 ----------
lin = db.q("SELECT * FROM voc_opp_lineage WHERE proposal_id=%s", [pid])
chk("写入血缘记录", len(lin) == 1)
if lin:
    chk("血缘方向正确（parent 含两者，child 为目标）",
        set(lin[0]["parent_ids"]) == {"EX-TARGET", "EX-SOURCE"}
        and lin[0]["child_ids"] == ["EX-TARGET"])
    chk("血缘记录裁决人（非 machine）", lin[0]["decided_by"] == "dev:tester",
        str(lin[0]["decided_by"]))

# ---------- 4. 幂等 ----------
stat2 = execute.run(W)
chk("重跑不重复执行（幂等）", stat2["MERGE"] == 0 and stat2["skipped"] >= 1, str(stat2))
chk("重跑后血缘仍只有 1 条",
    db.q1("SELECT count(*) FROM voc_opp_lineage WHERE proposal_id=%s", [pid]) == 1)

# ---------- 5. 安全类不得机器合并 ----------
db.execute("""INSERT INTO voc_opportunity(opp_id,opp_type,src_line,core_tag,title,
                problem_mode,safety_flag,first_week,last_week)
              VALUES('EX-SAFE','老品迭代','线A','耐用性','安全条目：支撑腿断裂',
                     '支撑腿在正常承重下断裂',true,'2026-W20','2026-W20')""")
db.execute("""INSERT INTO voc_opportunity(opp_id,opp_type,src_line,core_tag,title,
                problem_mode,first_week,last_week)
              VALUES('EX-SAFE2','老品迭代','线A','耐用性','另一条：腿部开裂',
                     '腿部在受力后开裂','2026-W21','2026-W21')""")
sid = db.q1("""INSERT INTO voc_proposal(op_type,opp_ids,rationale,week,status,decided_by)
               VALUES('MERGE',ARRAY['EX-SAFE','EX-SAFE2'],'疑似同一问题',%s,
                      'accepted','machine') RETURNING proposal_id""", [W])
stat3 = execute.run(W)
blocked = db.q1("SELECT count(*) FROM voc_opp_lineage WHERE proposal_id=%s", [sid]) == 0
chk("安全类被 machine 合并时由触发器拦截", blocked and stat3["failed"] >= 1, str(stat3))

cleanup()
print(f"\nM4 验收: PASS={P} FAIL={F}")
sys.exit(1 if F else 0)
