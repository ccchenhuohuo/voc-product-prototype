"""执行 PM 已通过的提案（二期 PRD §6.3）。

为什么必须在机器侧：MERGE / SPLIT 要改 voc_opportunity 与 voc_opp_evidence，
而看板应用连的是 voc_human，按 004_roles.sql 对机器表【无写权限】。这不是
绕不开的限制，而是有意的设计——人只裁决，机器才动数据。所以裁决是异步的：
PM 点通过 → status='accepted' → 下次周度运行由本模块消费执行。

一期的缺口：全库无任何代码消费 accepted，voc_opp_lineage 从建库起一行未写，
提案闭环从未合上。本模块补上这一环。

幂等：已写过 lineage 的 proposal_id 不再执行，重跑安全。
"""
from __future__ import annotations

from . import db, pipeline


def _done(proposal_id: int, c=None) -> bool:
    if c is not None:
        return bool(c.execute(
            "SELECT 1 FROM voc_opp_lineage WHERE proposal_id=%s LIMIT 1",
            [proposal_id]).fetchone())
    return bool(db.q1("SELECT 1 FROM voc_opp_lineage WHERE proposal_id=%s LIMIT 1",
                      [proposal_id]))


def _merge(p: dict, c) -> dict | None:
    """把源条目的证据并入目标，源条目标记为已弃用。

    若提案生产方提供 payload.target 则按其方向执行；当前生产生成路径并未写入
    该字段，因此通常退化为「先出现的作目标」——first_week 早的一方语义更稳定。
    安全类的自动合并由 trg_voc_guard_safety 在 lineage 写入时兜底拦截。
    """
    ids = p["opp_ids"] or []
    if len(ids) < 2:
        return None
    payload = p.get("payload") or {}
    target = payload.get("target")
    if target not in ids:
        rows = c.execute("""SELECT opp_id FROM voc_opportunity WHERE opp_id = ANY(%s)
                             ORDER BY first_week NULLS LAST, opp_id LIMIT 1""", [ids]).fetchall()
        if not rows:
            return None
        target = rows[0]["opp_id"]
    sources = [i for i in ids if i != target]
    if not sources:
        return None

    # 必须在改写外键关系前一次按稳定顺序锁住所有参与者；
    # 否则与并发 attach/MERGE 各自回算时会丢失对方的未提交关系。
    pipeline.lock_opportunities(c, [target, *sources])

    # 证据改挂到目标；主键冲突说明两侧本就共享该条证据，跳过即可
    c.execute("""
        INSERT INTO voc_opp_evidence
               (opp_id, message_id, seq, attach_week, match_by, confidence,
                assigned_spu, assignment_source, assign_run_id)
        SELECT %s, message_id, seq, attach_week, 'merge', confidence,
               assigned_spu, assignment_source, assign_run_id
          FROM voc_opp_evidence WHERE opp_id = ANY(%s)
        ON CONFLICT (opp_id, message_id, seq) DO NOTHING""", [target, sources])
    c.execute("DELETE FROM voc_opp_evidence WHERE opp_id = ANY(%s)", [sources])
    # 源条目不物理删除：voc_opportunity_manual 的外键【没有】CASCADE，
    # 删掉会连带毁掉人工决策。改为移出 PM 视野并标注去向。
    c.execute("""UPDATE voc_opportunity
                    SET backlog = true, merged_into = %s, updated_at = now()
                  WHERE opp_id = ANY(%s)""", [target, sources])
    # MERGE 同时改变目标与源条目的完整证据集：目标可能 R2→R1，
    # 源条目清空后必须成为 R0。在当前事务内回算才能看见未提交的改挂。
    pipeline.recount(target, c)
    for source in sources:
        pipeline.recount(source, c)
    return {"parent_ids": ids, "child_ids": [target], "target": target,
            "sources": sources}


def _split(p: dict, c) -> dict | None:
    """拆分【不由机器执行】：切成几份、怎么切是产品判断，机器给不出。

    通过即意味着「下次生成时该桶重新分组」，这里只记血缘留痕并解除
    backlog 抑制，让 PM 在列表里能继续看到它。
    """
    ids = p["opp_ids"] or []
    if not ids:
        return None
    return {"parent_ids": ids, "child_ids": ids}


def run_auto(week: str, *, opp_id_prefix: str | None = None) -> dict:
    """静默融合：双方都没被 PM 触碰过的 pending 提案，不值得打扰任何人
    （冻结设计稿 §3）。没有人工投入需要保护，融错的代价与生成期挂错同级，
    本来就在承受。安全类除外——写 lineage 时 trg_voc_guard_safety 兜底，
    这里提前跳过避免无谓的失败记录。

    做法：直接把提案置为 accepted（decided_by='machine:auto'，理由留痕），
    随后由 run() 走统一的执行路径——只有一条执行代码，不搞两套。
    """
    pattern = f"{opp_id_prefix}%" if opp_id_prefix is not None else None
    n = db.execute("""
        UPDATE voc_proposal p
           SET status='accepted', decided_by='machine:auto', decided_at=now(),
               decision_note='双方均未被 PM 认领，按冻结规则静默执行'
         WHERE p.status='pending' AND p.op_type IN ('MERGE','SPLIT')
           AND NOT EXISTS (SELECT 1 FROM voc_opportunity_manual m
                            WHERE m.opp_id = ANY(p.opp_ids))
           AND NOT EXISTS (SELECT 1 FROM voc_opportunity o
                            WHERE o.opp_id = ANY(p.opp_ids) AND o.safety_flag)
           AND (%s IS NULL OR NOT EXISTS (
                 SELECT 1 FROM unnest(p.opp_ids) x(opp_id)
                  WHERE x.opp_id NOT LIKE %s
               ))""", [pattern, pattern])
    return {"auto_accepted": n or 0}


def run(week: str, *, opp_id_prefix: str | None = None) -> dict:
    """消费全部 accepted 且未执行的提案。返回执行统计。"""
    stat = {"MERGE": 0, "SPLIT": 0, "skipped": 0, "failed": 0}
    pattern = f"{opp_id_prefix}%" if opp_id_prefix is not None else None
    rows = db.q("""SELECT proposal_id
                     FROM voc_proposal
                    WHERE status='accepted' AND op_type IN ('MERGE','SPLIT')
                      AND (%s IS NULL OR NOT EXISTS (
                            SELECT 1 FROM unnest(opp_ids) x(opp_id)
                             WHERE x.opp_id NOT LIKE %s
                          ))
                    ORDER BY proposal_id""", [pattern, pattern])
    for queued in rows:
        proposal_id = queued["proposal_id"]
        try:
            with db.conn() as c:
                c.execute("SELECT pg_advisory_xact_lock(%s)", [proposal_id])
                # 进入锁后必须重读：PM 可能已在运行开始后撤回该决定。
                p = c.execute("""SELECT proposal_id, op_type, opp_ids, payload,
                                          rationale, decided_by
                                     FROM voc_proposal
                                    WHERE proposal_id=%s AND status='accepted'
                                    FOR UPDATE""", [proposal_id]).fetchone()
                if not p or _done(proposal_id, c):
                    stat["skipped"] += 1
                    continue
                res = _merge(p, c) if p["op_type"] == "MERGE" else _split(p, c)
                if not res:
                    stat["skipped"] += 1
                    continue
                # 机会变更与 lineage 在同一事务中提交，失败会一起回滚。
                c.execute("""
                    INSERT INTO voc_opp_lineage
                           (op_type, parent_ids, child_ids, week, reason, proposal_id, decided_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                          [p["op_type"], res["parent_ids"], res["child_ids"], week,
                           p["rationale"][:500], p["proposal_id"],
                           p["decided_by"] or "pm"])
                stat[p["op_type"]] += 1
        except Exception as e:  # noqa: BLE001 —— 单条失败不阻断其余提案
            stat["failed"] += 1
            db.execute("""UPDATE voc_proposal
                             SET decision_note = COALESCE(decision_note,'') ||
                                 %s WHERE proposal_id=%s""",
                       [f" [执行失败: {str(e)[:160]}]", proposal_id])
    return stat
