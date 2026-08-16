"""L1 → L2 → L3 消解 + 跨线汇聚（PRD v8 §6）。

实测结论决定了这里的设计：
  · 向量排序极准（R@1 15/15）但阈值不可用（应挂载 min 0.656 < 应新建 max 0.706）
    => L2 只做桶内 Top-k 召回，判定权全部交给 L3
  · 线B 的 category 恒为 NULL => L1 必须分线定义，且比较用 IS NOT DISTINCT FROM
  · 没有跨线汇聚则 dual_source 恒为 false，探真里 5/15 的双源印证复现不出
"""
from __future__ import annotations
import math
from typing import Sequence

from . import config as C, db, llm, prompts


def topk(n_candidates: int) -> int:
    return min(C.L2_TOPK_MAX, max(C.L2_TOPK_MIN,
                                  math.ceil(n_candidates * C.L2_TOPK_RATIO)))


# ---------------------------------------------------------------- L1
def l1_candidates(core_tag: str, opp_type: str, channel: str | None,
                  category: str | None, line: str) -> list[dict]:
    """线A: (core_tag, opp_type)；线B: (channel, core_tag, opp_type)。
    一律用 IS NOT DISTINCT FROM，避免 NULL 语义陷阱。"""
    if line == "线A":
        sql = """SELECT opp_id, problem_mode, title, rep_snippets, mode_vec::text AS vec,
                        safety_flag, evi_total
                   FROM voc_opportunity
                  WHERE core_tag IS NOT DISTINCT FROM %s
                    AND opp_type IS NOT DISTINCT FROM %s"""
        params: list = [core_tag, opp_type]
    else:
        sql = """SELECT opp_id, problem_mode, title, rep_snippets, mode_vec::text AS vec,
                        safety_flag, evi_total
                   FROM voc_opportunity
                  WHERE channel IS NOT DISTINCT FROM %s
                    AND core_tag IS NOT DISTINCT FROM %s
                    AND opp_type IS NOT DISTINCT FROM %s"""
        params = [channel, core_tag, opp_type]
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
    try:
        obj, meta = llm.chat_json(prompt, max_tokens=400, required=["verdict"])
        ctx.bump(tokens=meta.get("tokens", 0))
        return {"verdict": obj.get("verdict"), "confidence": float(obj.get("confidence") or 0),
                "rationale": obj.get("rationale", ""), "opp_id": cand["opp_id"],
                "safety": bool(cand.get("safety_flag"))}
    except Exception as e:  # noqa: BLE001
        return {"verdict": "different", "confidence": 0.0,
                "rationale": f"L3 调用失败，保守判为不同: {str(e)[:100]}",
                "opp_id": cand["opp_id"], "safety": bool(cand.get("safety_flag"))}


def resolve_one(new: dict, ctx) -> dict:
    """对一个新产出的机会点做消解。返回 {action, opp_id, proposals}"""
    cands = l1_candidates(new["core_tag"], new["opp_type"], new.get("channel"),
                          new.get("category"), new["src_line"])
    if not cands:
        ctx.metrics.setdefault("resolve", {}).setdefault("l1_empty", 0)
        ctx.metrics["resolve"]["l1_empty"] += 1
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


# ---------------------------------------------------------------- 跨线汇聚
def cross_line_merge(opp_id: str, mode_vec: Sequence[float], core_tag: str,
                     src_line: str, week: str, ctx, limit: int = 40) -> int:
    """从另一条线的证据池召回并挂载。这是 dual_source 成立的唯一途径（§6.5）。"""
    if src_line == "线B":
        # 线B 机会点 → 借道【线A 机会点】取其证据。
        #
        # 早期这里直接检索线A 证据、并要求 e.tag = 本条的 core_tag，实测
        # 350 条线B 机会点命中 0 条：线B 的 core_tag 是 Stage1 的自由文本
        # 模式名（「三色温平价冷靴灯」「TT8同颜值高矮轻量三脚架」），而线A 的
        # tag 是分类树叶子（「RGB」「三脚」「APP控制」），两个取值域根本不相交，
        # 等值比较恒假。冷启动 dual_source 只有 1 条就是这么来的。
        #
        # 改为对线A【机会点】做向量召回：两条线的 problem_mode 都已入库为
        # mode_vec 且有 pgvector 索引，同一个问题在两条线的表述才是可比的；
        # 命中后把那条线A 机会点的证据挂过来，dual_source 由触发器自然派生。
        rows = db.q("""
            SELECT oe.message_id, oe.seq,
                   COALESCE(e.snippet, left(m.content,400)) AS snippet
              FROM voc_opportunity a
              JOIN voc_opp_evidence oe ON oe.opp_id = a.opp_id
              JOIN voc_message m ON m.message_id = oe.message_id
              LEFT JOIN voc_evidence e
                     ON e.message_id = oe.message_id AND e.seq = oe.seq
             WHERE a.src_line = '线A' AND a.mode_vec IS NOT NULL
               AND m.src_line = '电商'
               AND a.opp_id IN (SELECT opp_id FROM voc_opportunity
                                 WHERE src_line='线A' AND mode_vec IS NOT NULL
                                 ORDER BY mode_vec <=> %s::vector LIMIT 5)
               AND NOT EXISTS (SELECT 1 FROM voc_opp_evidence x
                                WHERE x.opp_id=%s AND x.message_id=oe.message_id
                                  AND x.seq=oe.seq)
               AND COALESCE(e.snippet, m.content) IS NOT NULL
             LIMIT %s""", [_vec_literal(mode_vec), opp_id, limit])
        texts = [r["snippet"] for r in rows]
    else:
        # 线A 机会点 → 检索线B 的诉求/对标池
        rows = db.q("""
            SELECT m.message_id, 0 AS seq, left(m.content, 400) AS snippet
              FROM voc_message m
             WHERE m.src_line='社媒' AND m.content_type && %s
               AND NOT EXISTS (SELECT 1 FROM voc_opp_evidence oe
                                WHERE oe.opp_id=%s AND oe.message_id=m.message_id)
             ORDER BY m.interactions DESC NULLS LAST LIMIT %s""",
                    [list(C.DEWATER_GAP | C.DEWATER_COMP), opp_id, limit])
        texts = [r["snippet"] for r in rows]

    if not texts:
        return 0
    vecs = llm.embed(texts)
    scored = sorted(((llm.cosine(mode_vec, v), i) for i, v in enumerate(vecs)), reverse=True)
    attached = 0
    for score, i in scored[:5]:            # 只让最像的 5 条进 L3，控制成本
        r = rows[i]
        # 复用 L3：判断该证据是否支撑同一问题
        v = l3_verdict(texts[i][:200], "", [texts[i]],
                       {"opp_id": opp_id,
                        "problem_mode": db.q1("SELECT problem_mode FROM voc_opportunity WHERE opp_id=%s",
                                              [opp_id]) or "",
                        "title": db.q1("SELECT title FROM voc_opportunity WHERE opp_id=%s",
                                       [opp_id]) or "",
                        "rep_snippets": []}, ctx)
        if v["verdict"] == "same" and v["confidence"] >= 0.6:
            db.upsert("voc_opp_evidence",
                      [{"opp_id": opp_id, "message_id": r["message_id"], "seq": r["seq"],
                        "attach_week": week, "match_by": "cross_line",
                        "confidence": round(min(v["confidence"], 0.999), 3)}],
                      ["opp_id", "message_id", "seq"])
            attached += 1
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
