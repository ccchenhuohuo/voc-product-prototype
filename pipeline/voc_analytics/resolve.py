"""L1 → L2 → L3 消解 + 跨来源补证（PRD v8 §6）。

实测结论决定了这里的设计：
  · 向量排序极准（R@1 15/15）但阈值不可用（应挂载 min 0.656 < 应新建 max 0.706）
    => L2 只做桶内 Top-k 召回，判定权全部交给 L3
  · L1 的键形状由机会生命周期决定，来源不参与候选分区
  · 补证严格限制在同一生命周期，来源数量可以自然扩展
"""
from __future__ import annotations
import math
from typing import Sequence

from . import config as C, db, llm, prompts
from .classification import classify_evidence


def topk(n_candidates: int) -> int:
    return min(C.L2_TOPK_MAX, max(C.L2_TOPK_MIN,
                                  math.ceil(n_candidates * C.L2_TOPK_RATIO)))


# ---------------------------------------------------------------- L1
def l1_candidates(core_tag: str, opp_type: str,
                  channel: str | None) -> list[dict]:
    """按生命周期选择 L1 键；来源名称永远不参与分支。"""
    if opp_type == "老品迭代":
        sql = """SELECT opp_id, problem_mode, title, rep_snippets, mode_vec::text AS vec,
                        safety_flag, evi_total
                   FROM voc_opportunity
                  WHERE core_tag IS NOT DISTINCT FROM %s
                    AND opp_type IS NOT DISTINCT FROM %s
                    AND classification_state = '确定'
                    AND merged_into IS NULL"""
        params: list = [core_tag, opp_type]
    elif opp_type == "新品创新":
        sql = """SELECT opp_id, problem_mode, title, rep_snippets, mode_vec::text AS vec,
                        safety_flag, evi_total
                   FROM voc_opportunity
                  WHERE channel IS NOT DISTINCT FROM %s
                    AND core_tag IS NOT DISTINCT FROM %s
                    AND opp_type IS NOT DISTINCT FROM %s
                    AND classification_state = '确定'
                    AND merged_into IS NULL"""
        params = [channel, core_tag, opp_type]
    else:
        raise ValueError(f"未知机会类型：{opp_type!r}")
    return db.q(sql, params)


def _parse_vec(s: str | None) -> list[float] | None:
    if not s:
        return None
    return [float(x) for x in s.strip("[]").split(",")] if s.strip("[]") else None


def _vec_literal(v: Sequence[float]) -> str:
    """pgvector 的文本字面量：'[0.1,0.2,…]'，供 <=> 运算符走索引。"""
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"


# ---------------------------------------------------------------- L2
def l2_rank(new_vec: Sequence[float], candidates: list[dict]) -> list[tuple[float, dict]]:
    scored = []
    for c in candidates:
        v = _parse_vec(c.get("vec"))
        if v:
            scored.append((llm.cosine(new_vec, v), c))
    scored.sort(key=lambda x: -x[0])
    return scored[:topk(len(candidates))]


# ---------------------------------------------------------------- L3
def l3_verdict(new_mode: str, new_title: str, new_snips: Sequence[str],
               cand: dict, ctx) -> dict:
    prompt = prompts.L3_VERDICT.format(
        a_mode=cand.get("problem_mode") or "", a_title=cand.get("title") or "",
        a_snips=" / ".join((cand.get("rep_snippets") or [])[:3]) or "-",
        b_mode=new_mode, b_title=new_title,
        b_snips=" / ".join(list(new_snips)[:3]) or "-")
    obj, meta = llm.chat_json(prompt, max_tokens=400, required=["verdict"])
    ctx.bump(tokens=meta.get("tokens", 0))
    return {"verdict": obj.get("verdict"), "confidence": float(obj.get("confidence") or 0),
            "rationale": obj.get("rationale", ""), "opp_id": cand["opp_id"],
            "safety": bool(cand.get("safety_flag"))}


def resolve_one(new: dict, ctx) -> dict:
    """对一个新产出的机会点做消解。返回 {action, opp_id, proposals}"""
    cands = l1_candidates(new["core_tag"], new["opp_type"], new.get("channel"))
    if not cands:
        ctx.metric_incr(("resolve",), l1_empty=1)
        return {"action": "create", "opp_id": None, "proposals": []}

    ranked = l2_rank(new["mode_vec"], cands)
    sames = []
    for score, c in ranked:
        v = l3_verdict(new["problem_mode"], new["title"], new.get("rep_snippets") or [], c, ctx)
        v["l2_score"] = round(score, 4)
        if v["verdict"] == "same":
            sames.append(v)

    if not sames:
        return {"action": "create", "opp_id": None, "proposals": []}

    sames.sort(key=lambda x: -x["confidence"])
    best = sames[0]
    proposals = []
    # 安全类不自动合并（§10.1）
    if best["safety"]:
        return {"action": "create", "opp_id": None, "proposals": [
            {"op_type": "MERGE", "opp_ids": [best["opp_id"]],
             "rationale": f'安全类条目不自动合并，L3 判定 same（{best["rationale"]}）'}]}
    # 多命中：其余进提案（§6.6）
    for extra in sames[1:]:
        proposals.append({"op_type": "MERGE", "opp_ids": [best["opp_id"], extra["opp_id"]],
                          "rationale": f'同一新条目同时命中，需人工确认：{extra["rationale"]}'})
    return {"action": "attach", "opp_id": best["opp_id"], "confidence": best["confidence"],
            "proposals": proposals}


# ---------------------------------------------------------------- 跨来源补证
def cross_source_merge(opp_id: str, mode_vec: Sequence[float], opp_type: str,
                       source_lines: Sequence[str], week: str, ctx,
                       limit: int = 40) -> list[dict]:
    """从同生命周期、其他来源的机会点召回证据；不做来源二分。"""
    sources = sorted(set(source_lines))
    target = db.q("""SELECT problem_mode, title, rep_snippets
                       FROM voc_opportunity WHERE opp_id=%s""", [opp_id])
    if not target:
        return []
    candidates = db.q("""
      SELECT o.opp_id, o.problem_mode, o.title, o.rep_snippets,
             o.safety_flag, o.evi_total, o.mode_vec::text AS vec
        FROM voc_opportunity o
       WHERE o.opp_id <> %s
         AND o.opp_type = %s
         AND o.classification_state = '确定'
         AND o.merged_into IS NULL
         AND o.mode_vec IS NOT NULL
         AND EXISTS (
           SELECT 1
             FROM voc_opp_evidence oe
             JOIN voc_message m USING (message_id)
            WHERE oe.opp_id = o.opp_id
              AND NOT (m.src_line = ANY(%s)))
       ORDER BY o.mode_vec <=> %s::vector
       LIMIT 5
    """, [opp_id, opp_type, sources, _vec_literal(mode_vec)])

    attached: list[dict] = []
    for cand in candidates:
        verdict = l3_verdict(
            target[0].get("problem_mode") or "", target[0].get("title") or "",
            target[0].get("rep_snippets") or [], cand, ctx)
        if verdict["verdict"] != "same" or verdict["confidence"] < 0.6:
            continue
        remaining = max(limit - len(attached), 0)
        if not remaining:
            break
        rows = db.q("""
          SELECT oe.message_id, oe.seq, m.spu,
                 p.requires_spu AS source_requires_spu
            FROM voc_opp_evidence oe
            JOIN voc_message m USING (message_id)
            JOIN voc_source_policy p USING (src_line)
           WHERE oe.opp_id=%s
             AND NOT (m.src_line = ANY(%s))
             AND NOT EXISTS (
               SELECT 1 FROM voc_opp_evidence x
                WHERE x.opp_id=%s AND x.message_id=oe.message_id AND x.seq=oe.seq)
           ORDER BY oe.message_id, oe.seq
        """, [cand["opp_id"], sources, opp_id])
        eligible = [
            row for row in rows
            if classify_evidence((row,)).opp_type == opp_type
        ][:remaining]
        attached.extend(
            {"opp_id": opp_id, "message_id": row["message_id"], "seq": row["seq"],
             "attach_week": week, "match_by": "cross_line",
             "confidence": round(min(verdict["confidence"], 0.999), 3)}
            for row in eligible)
    return attached


# ---------------------------------------------------------------- 拆分检测
def detect_split(opp_id: str) -> dict | None:
    """两条确定性规则（§6.7）。非主品类占比 >20% 即提议拆分。"""
    rows = db.q("""
        SELECT m.category, count(*) n
          FROM voc_opp_evidence oe JOIN voc_message m USING(message_id)
         WHERE oe.opp_id=%s AND m.category IS NOT NULL
         GROUP BY 1 ORDER BY n DESC""", [opp_id])
    total = sum(r["n"] for r in rows)
    if total < 8 or len(rows) < 2:
        return None
    minor = sum(r["n"] for r in rows[1:])
    ratio = minor / total
    if ratio > 0.20:
        return {"op_type": "SPLIT", "opp_ids": [opp_id],
                "rationale": f"品类分布分裂：主品类 {rows[0]['category']} 占 "
                             f"{(1-ratio)*100:.0f}%，非主品类合计 {ratio*100:.0f}% "
                             f"（{', '.join(r['category'] for r in rows[1:])}），超过 20% 阈值"}
    return None
