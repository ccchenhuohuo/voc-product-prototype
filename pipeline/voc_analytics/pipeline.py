"""生成与消解编排（PRD v8 §4.3–4.4 / §5 / §6）。"""
from __future__ import annotations
import hashlib, re
from collections import defaultdict
from typing import Sequence

from . import config as C, db, llm, prompts, resolve
from .stages import generate, stage1, validate


def _opp_id(line: str, key: str) -> str:
    h = hashlib.sha1(f"{line}|{key}".encode()).hexdigest()[:10]
    return f"OPP-{h.upper()}"


def _no_placeholder(name: str | None) -> str | None:
    """兜底占位符是 Stage1 的内部状态，不是语义；落库前一律抹掉。"""
    return None if not name or name == stage1.PLACEHOLDER_MODE else name


def _specific_mode(obj: dict, mode_name: str) -> str | None:
    """落库前保证 problem_mode 具体可用。

    校验层已经会因分类名打回并重试，这里是重试耗尽后的最后一道：宁可退回
    Stage1 的模式名或标题，也不能让「需求缺口类」这种分类名进 mode_vec——
    几百条算出同一个向量，L2/L3/墓碑/跨线全部失效（实测 593 条只有 63 个不同值）。
    """
    for cand in (obj.get("problem_mode"), _no_placeholder(mode_name), obj.get("title")):
        c = (cand or "").strip()
        if c and not validate.check_problem_mode({"problem_mode": c}):
            return c
    # 全都不合格时用标题兜底：它至少是具体的，不会几百条撞成一个向量
    return (obj.get("title") or "").strip() or None


def _rep_snips(items: list[dict], members: Sequence[int], k: int = 5) -> list[str]:
    out = []
    for i in members:
        s = (items[i].get("snippet") or items[i].get("content") or "").strip()
        s = re.sub(r"\s+", " ", s)[:200]
        if s and s not in out:
            out.append(s)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------- 电商
def bucket_line_a(week_start: str | None = None, week_end: str | None = None) -> dict:
    rows = db.line_a_pool(week_start, week_end)
    buckets: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        buckets[(r.get("category") or "未知", r["tag"])].append(r)
    return dict(buckets)


# ---------------------------------------------------------------- 社媒诉求门
def intent_gate(rows: list[dict], ctx) -> tuple[list[dict], dict]:
    """规则门之后的 LLM 二分类。低置信也放行（R11：宁可多看不可漏）。"""
    RULE = re.compile(
        r"能不能|能否|可不可以|可以出|出个|出一个|出一款|什么时候出|什麼時候|啥时候|"
        r"希望|建议|求个|求一个|多研发|单独(出|做|购买|卖|买)|考虑.*出|意向.*出|"
        r"为什么不能|為什麼不能|设想|可行性|衍生产品|"
        r"is there any (option|plan|way)|will there be|any plan|hope.*(add|make)|"
        r"wish.*(had|would)|could you (add|make)|should (make|add)|improvement would be",
        re.I)
    pre = [r for r in rows if RULE.search(r.get("content") or "")]
    stats = {"total": len(rows), "rule_pass": len(pre), "intent": defaultdict(int)}

    def classify(r: dict) -> dict | None:
        try:
            obj, meta = llm.chat_json(prompts.INTENT_GATE.format(
                content=(r.get("content") or "")[:800],
                platform=r.get("platform"), interactions=r.get("interactions") or 0,
                brands=",".join(r.get("brands") or []) or "-"),
                max_tokens=250, required=["intent"])
            ctx.bump(tokens=meta.get("tokens", 0))
            return {**r, "_intent": obj.get("intent"),
                    "_conf": float(obj.get("confidence") or 0)}
        except Exception:  # noqa: BLE001
            return None

    out = []
    for res in llm.parallel_map(classify, pre):
        if not isinstance(res, dict):
            continue
        stats["intent"][res["_intent"]] += 1
        if res["_intent"] == "需求缺口" and res["_conf"] >= C.INTENT_REVIEW:
            res["_needs_review"] = res["_conf"] < C.INTENT_PASS
            out.append(res)
    stats["intent"] = dict(stats["intent"])
    stats["passed"] = len(out)
    ctx.metrics.setdefault("intent_gate", {}).update(stats)
    return out, stats


# ---------------------------------------------------------------- 生成一个机会点
def build_opportunity(items: list[dict], group: dict, line: str, ctx_info: dict,
                      ctx, history: list[str]) -> dict | None:
    members = group["members"]
    # 兜底占位符不能进 prompt——模型会照抄进标题（实测产出过
    # 「摄影配件品类：填补未命名模式（用户自定义功能组合）」）。
    # 命名失败时让 Stage2 自己从证据里概括，比塞一个空洞的名字更好。
    mode_name = group["mode_name"]
    if mode_name == stage1.PLACEHOLDER_MODE:
        mode_name = "（未命名，请自行从证据中概括）"
    obj = generate.write_prototype(items, members, mode_name, line, ctx_info, ctx)
    if obj is None:
        return None
    sugg, ctx_hash = generate.write_suggestion(
        obj.get("title", ""), obj.get("desc_phenomenon", ""),
        obj.get("desc_attribution", ""), history, ctx)
    review = generate.llm_review(obj, sugg, items, members, line, ctx)
    # needs_review 【只由程序化校验决定】。LLM 复核降级为提示信息落库供抽查。
    #
    # 依据是实测：复核明确被告知「聚合/否定/归纳陈述不算问题」后仍照列；
    # 更严重的是它会把【原声引用本身】判为幻觉（如把证据原文"隔着手机壳也吸不上"
    # 列为无法溯源）；输出还常被 max_tokens 截断导致解析失败。
    # 「找出问题」这类开放式任务天然倾向产出非空结果，是 LLM-as-judge 的固有偏差。
    #
    # 程序化校验（citations 溯源 / 标题公式 / 字段泄漏 / 禁用词 / 标准号）是
    # 确定性、可单测、已验证有效的，它才应该是硬闸门。这也是 §5.9 的原意——
    # 复核是「独立视角兜底」，兜底不该有一票否决权。
    hard_issues = [i for i in (review.get("issues") or [])
                   if i.get("segment") in ("phenomenon", "attribution")]
    review["hard_issues"] = hard_issues

    stars = [items[i]["star"] for i in members if items[i].get("star") is not None]
    low_rate = (sum(1 for s in stars if s <= 2) / len(stars)) if stars else None
    cats = sorted({items[i].get("category") for i in members if items[i].get("category")})
    safety_idx = obj.get("safety_evidence_idx") or []
    safety_ids = [f'{items[members[j-1]]["message_id"]}:{items[members[j-1]].get("seq",0)}'
                  for j in safety_idx if isinstance(j, int) and 1 <= j <= len(members)]

    meta = obj.get("_meta") or {}
    return {
        # 键里必须含 problem_mode —— 早期只用 (category, tag, mode_name)，同桶内
        # 两个未被归并的同名组会算出【完全相同的 opp_id】，upsert 静默相互覆盖、
        # 证据却并集累加，产出「evi_total=91 而描述只讲了 1 条」的错乱行。
        # 跨周身份不靠这个哈希维持，靠 resolve_one 的 attach 路径（§6.3）。
        "opp_id": _opp_id(line, f'{ctx_info.get("category","")}|'
                                f'{ctx_info.get("tag") or ctx_info.get("channel","")}|'
                                f'{group["mode_name"]}|'
                                f'{obj.get("problem_mode") or obj.get("title") or ""}'),
        "opp_type": "老品迭代" if line == "电商" else "新品创新",
        "src_line": line,
        "channel": ctx_info.get("channel"),
        "prod_line": ctx_info.get("prod_line"),
        "category": ctx_info.get("category") if line == "电商" else "SOCIAL-NA",
        "category_set": cats or None,
        # 兜底占位符不得落库：problem_mode 是向量化字段，写成「未命名模式」会让
        # 该条在 L2 召回里和什么都像，重演吸附器问题。
        "core_tag": ctx_info.get("tag") or _no_placeholder(group["mode_name"]),
        "problem_mode": _specific_mode(obj, group["mode_name"]),
        "title": obj.get("title"),
        "desc_phenomenon": obj.get("desc_phenomenon"),
        "desc_attribution": obj.get("desc_attribution"),
        "desc_suggestion": sugg,
        "safety_flag": bool(obj.get("safety_flag")),
        "safety_evidence_ids": safety_ids or None,
        "evi_total": len(members),
        "evi_ec": sum(1 for i in members if items[i].get("star") is not None
                      or items[i].get("category")),
        "evi_social": 0,
        "low_star_rate": round(low_rate, 4) if low_rate is not None else None,
        "countries": sorted({items[i].get("country") for i in members
                             if items[i].get("country")}) or None,
        "product_names": sorted({items[i].get("product_name") for i in members
                                 if items[i].get("product_name")})[:5] or None,
        "rep_snippets": _rep_snips(items, members),
        "prompt_ver": prompts.VER,
        "model_id": meta.get("model_id"),
        "model_ver": meta.get("model_ver"),
        "ctx_hash": ctx_hash,
        "needs_review": bool(obj.get("_needs_review")),
        "review_notes": {"hard": hard_issues,
                         "soft": review.get("soft_issues") or [],
                         "error": review.get("review_error")} if (
            hard_issues or review.get("soft_issues") or review.get("review_error")) else None,
        "_members": members,
        "_review": review,
    }


# ---------------------------------------------------------------- 落库
# 这三个函数原先只存在于 scripts/run_generate.py 里，Dagster 的两个生成资产
# 把机会点 build 出来后只是累加进列表就返回了，【从不落库】。也就是说周度
# 调度这条路径跑完什么都没写进去。与 lifecycle.release_to_pm 是同一类问题：
# 逻辑写在脚本里、调度器绕过脚本。提到这里由两条路径共用。
def recount(opp_id: str) -> None:
    """按已挂载证据重算计数与排序分。"""
    db.execute("""
      UPDATE voc_opportunity o SET
        evi_total = s.n, evi_ec = s.ec, evi_social = s.sc,
        low_star_rate = s.lsr,
        rank_score = (s.n * (1 + COALESCE(s.lsr,0)))
                     * CASE WHEN s.ec>0 AND s.sc>0 THEN 1.5 ELSE 1 END
                     + CASE WHEN o.safety_flag THEN 1000 ELSE 0 END
      FROM (SELECT oe.opp_id,
                   count(*) n,
                   count(*) FILTER (WHERE m.src_line='电商') ec,
                   count(*) FILTER (WHERE m.src_line='社媒') sc,
                   avg(CASE WHEN m.star<=2 THEN 1.0 WHEN m.star IS NULL THEN NULL ELSE 0 END) lsr
              FROM voc_opp_evidence oe JOIN voc_message m USING(message_id)
             WHERE oe.opp_id=%s GROUP BY 1) s
      WHERE o.opp_id=s.opp_id""", [opp_id])


def save_opportunity(opp: dict, items: list[dict], week: str, ctx) -> str:
    """消解（新建 or 挂到已有条目）后落库，返回最终 opp_id。"""
    members = opp.pop("_members")
    opp.pop("_review", None)
    # 注意：不能 pop mode_vec —— 它必须随 opp 一起入库，否则 L2 召回与
    # 跨线汇聚（§6.5）全部失效。早期这里 pop 掉了，实测 mode_vec 0/40。
    dec = (resolve.resolve_one(opp, ctx) if opp.get("mode_vec") is not None
           else {"action": "create", "opp_id": None, "proposals": []})

    if dec["action"] == "attach":
        opp_id = dec["opp_id"]
        # 已有条目：只追加证据与统计，语义字段由触发器按锁级别决定是否放行
        db.execute("UPDATE voc_opportunity SET last_week=%s, updated_at=now() "
                   "WHERE opp_id=%s", [week, opp_id])
    else:
        opp_id = opp["opp_id"]
        opp["first_week"] = opp["last_week"] = week
        opp["backlog"] = True
        db.upsert("voc_opportunity",
                  [{k: v for k, v in opp.items() if not k.startswith("_")}], ["opp_id"])

    db.upsert("voc_opp_evidence",
              [{"opp_id": opp_id, "message_id": items[i]["message_id"],
                "seq": items[i].get("seq", 0), "attach_week": week,
                "match_by": "rule" if dec["action"] == "create" else "llm",
                "confidence": round(dec.get("confidence", 1.0), 3)} for i in members],
              ["opp_id", "message_id", "seq"])

    for p in dec.get("proposals", []):
        db.execute("INSERT INTO voc_proposal(op_type,opp_ids,rationale,week) "
                   "VALUES(%s,%s,%s,%s)",
                   [p["op_type"], p["opp_ids"], p["rationale"], week])
    return opp_id


def persist_opportunities(pairs: list, week: str, ctx, verbose: bool = False) -> list[str]:
    """批量向量化后落库。早期是逐条 llm.embed()，每条一次 HTTP 往返；
    改为按 EMBED_BATCH 批量（实测上限 10），调用次数降一个数量级。"""
    if not pairs:
        return []
    vecs = llm.embed([o["problem_mode"] for o, _ in pairs])
    out = []
    for (opp, items), vec in zip(pairs, vecs):
        opp["mode_vec"] = vec
        title = opp.get("title") or ""
        oid = save_opportunity(opp, items, week, ctx)
        recount(oid)
        out.append(oid)
        if verbose:
            print(f"      -> {oid}  {title[:46]}", flush=True)
    return out
