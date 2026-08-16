"""生命周期状态机的应用层逻辑（PRD v8 §7）。

触发器已在数据库层保证：
  · locked 条目的语义字段不可被机器改写（trg_voc_guard_locked）
  · 「不考虑」必填理由（trg_voc_log_status），状态变更写审计日志（trg_voc_audit_opp_status）
  · 安全类不得机器自动合并（trg_voc_guard_safety）

本模块补上应用层三件事：墓碑抑制、墓碑唤醒、陈旧检测。
"""
from __future__ import annotations
from . import db, resolve

LOCKED = ("在跟进", "项目中", "已完成", "不考虑")
PRIORITY = {"项目中": 5, "在跟进": 4, "已完成": 3, "考虑中": 2, "不考虑": 1}


# ---------------------------------------------------------------- 放行（§12.2）
def release_to_pm() -> dict:
    """安全类 / 双源印证 / rank Top-N 放行进 PM 视野，其余留 backlog。

    这段逻辑早期只写在 Dagster 的 release_to_pm 资产里，而冷启动是直接跑
    scripts/run_generate.py 绕过 Dagster 的，结果 593 条机会点全部留在
    backlog、PM 侧一条也看不到。提取到这里由两条路径共用，避免再次漏跑。

    needs_review 的条目【照常放行】：§3.4 的 voc_inbox 就是靠
    「needs_review AND NOT backlog」把它们送进 PM 的复核队列，
    留在 backlog 反而没人看得见。
    """
    from . import config as C
    db.execute("""
      WITH ranked AS (
        SELECT opp_id, row_number() OVER (ORDER BY rank_score DESC NULLS LAST) rn
          FROM voc_opportunity WHERE backlog)
      UPDATE voc_opportunity o SET backlog = false, released_at = now()
        FROM ranked r WHERE o.opp_id = r.opp_id
         AND (o.safety_flag OR o.dual_source OR r.rn <= %s)""", [C.BACKLOG_TOP_N])
    return {
        "已放行": db.q1("SELECT count(*) FROM voc_opportunity WHERE NOT backlog") or 0,
        "留 backlog": db.q1("SELECT count(*) FROM voc_opportunity WHERE backlog") or 0,
        "其中安全类": db.q1("SELECT count(*) FROM voc_opportunity "
                            "WHERE NOT backlog AND safety_flag") or 0,
        "其中待复核": db.q1("SELECT count(*) FROM voc_opportunity "
                            "WHERE NOT backlog AND needs_review") or 0,
    }


def status_of(opp_id: str) -> str:
    return db.q1("""SELECT COALESCE(m.status,'考虑中') FROM voc_opportunity o
                    LEFT JOIN voc_opportunity_manual m USING(opp_id)
                    WHERE o.opp_id=%s""", [opp_id]) or "考虑中"


def is_locked(opp_id: str) -> bool:
    return status_of(opp_id) in LOCKED


# ---------------------------------------------------------------- 墓碑
def tombstones(require_vec: bool = False) -> list[dict]:
    """「不考虑」的机会点。

    require_vec=True 用于【抑制】——需要向量做相似度召回。
    require_vec=False 用于【唤醒】——只看证据计数，不该被向量有无卡住，
    否则没跑过 embedding 的墓碑永远不会被复议。
    """
    sql = """
      SELECT o.opp_id, o.problem_mode, o.title, o.rep_snippets,
             o.mode_vec::text AS vec, o.core_tag, o.opp_type, o.channel,
             o.safety_flag, o.evi_total
        FROM voc_opportunity o JOIN voc_opportunity_manual m USING(opp_id)
       WHERE m.status='不考虑'"""
    if require_vec:
        sql += " AND o.mode_vec IS NOT NULL"
    return db.q(sql)


def check_tombstone(new: dict, ctx) -> dict | None:
    """新机会点是否命中墓碑。命中则抑制，但【证据仍要落库】——
    否则"累计到 3 倍"永远没有落点，REVIVE 永不触发（§7.3）。"""
    if new.get("safety_flag"):
        return None                     # 安全类不受墓碑抑制（§10.1）
    cands = [t for t in tombstones(require_vec=True)
             if t["core_tag"] == new.get("core_tag") and t["opp_type"] == new.get("opp_type")]
    if not cands:
        return None
    ranked = resolve.l2_rank(new["mode_vec"], cands)
    for score, c in ranked:
        v = resolve.l3_verdict(new["problem_mode"], new.get("title", ""),
                               new.get("rep_snippets") or [], c, ctx)
        if v["verdict"] == "same":
            return {"opp_id": c["opp_id"], "confidence": v["confidence"],
                    "rationale": v["rationale"]}
    return None


def tombstone_baseline(opp_id: str) -> int | None:
    """基准 = 最后一次置为「不考虑」那一周的快照证据数（§7.3）。"""
    return db.q1("""
      SELECT s.evi_total
        FROM voc_status_log l
        JOIN voc_opp_snapshot s ON s.opp_id = l.opp_id
       WHERE l.opp_id=%s AND l.to_status='不考虑'
         AND s.week <= to_char(l.changed_at, 'IYYY-"W"IW')
       ORDER BY l.changed_at DESC, s.week DESC LIMIT 1""", [opp_id])


def check_revive(week: str) -> int:
    """墓碑证据累计达基准 3 倍 => 提 REVIVE 提案。
    被拒后基准重置为当时的 evi_total，避免每周重复提案。"""
    n = 0
    for t in tombstones():
        base = tombstone_baseline(t["opp_id"])
        if not base:
            continue
        # 若上一条 REVIVE 已被拒，基准抬到被拒时的证据数
        rejected = db.q1("""
          SELECT (payload->>'evi_at_reject')::int FROM voc_proposal
           WHERE op_type='REVIVE' AND %s = ANY(opp_ids) AND status='rejected'
           ORDER BY decided_at DESC NULLS LAST LIMIT 1""", [t["opp_id"]])
        base = max(base, rejected or 0)
        cur = t["evi_total"] or 0
        if cur < base * 3:
            continue
        exists = db.q1("""SELECT 1 FROM voc_proposal
                           WHERE op_type='REVIVE' AND %s = ANY(opp_ids)
                             AND status='pending' LIMIT 1""", [t["opp_id"]])
        if exists:
            continue
        db.execute("""INSERT INTO voc_proposal(op_type,opp_ids,rationale,week,payload)
                      VALUES('REVIVE',%s,%s,%s,%s::jsonb)""",
                   [[t["opp_id"]],
                    f"墓碑证据累计 {cur} 条，已达置为「不考虑」时基准 {base} 条的 3 倍，"
                    f"建议复议：当时数据不足，现在够了",
                    week, f'{{"baseline":{base},"evi_at_reject":{cur}}}'])
        n += 1
    return n


# ---------------------------------------------------------------- 陈旧检测
def stale_items() -> list[dict]:
    """在跟进/项目中 且 >8 周未更新（§7.8）。"""
    return db.q("""
      SELECT o.opp_id, o.title, m.status, m.owner, m.updated_at
        FROM voc_opportunity o JOIN voc_opportunity_manual m USING(opp_id)
       WHERE m.status IN ('在跟进','项目中')
         AND m.updated_at < now() - interval '8 weeks'
       ORDER BY m.updated_at""")


def merge_direction(a: str, b: str) -> tuple[str, str, bool]:
    """返回 (目标, 源, 是否需人工确认)。低优先级 → 高优先级；
    目标 locked 或状态陈旧时只提提案（§7.4 约束一/二）。"""
    sa, sb = status_of(a), status_of(b)
    target, source = (a, b) if PRIORITY[sa] >= PRIORITY[sb] else (b, a)
    stale_ids = {x["opp_id"] for x in stale_items()}
    need_manual = status_of(target) in LOCKED or target in stale_ids
    return target, source, need_manual
