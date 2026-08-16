"""Stage 2 / 3 / 4：撰写 → 建议 → 校验（PRD v8 §5.7–5.9）。"""
from __future__ import annotations
import hashlib
import re
from typing import Any, Sequence

from .. import llm, prompts
from . import validate


def _evidence_text(item: dict, limit: int = 400) -> str:
    text = (item.get("evidence_text") or item.get("snippet")
            or item.get("content") or "")
    return str(text).replace("\n", " ")[:limit]


def _fmt_items(items: list[dict], idx: Sequence[int]) -> str:
    """逐条格式化混合来源证据，不用整组来源猜测字段。"""
    out = []
    for n, i in enumerate(idx, 1):
        it = items[i]
        meta: list[str] = []
        if it.get("star") is not None:
            star = it["star"]
            star_text = f"{star:g}" if isinstance(star, (int, float)) else str(star)
            meta.append(f"星级:{star_text}")
        if it.get("country"):
            meta.append(f'国家:{it["country"]}')
        if it.get("platform"):
            meta.append(f'平台:{it["platform"]}')
        brands = it.get("brands")
        if brands:
            if isinstance(brands, (list, tuple, set)):
                brands = ",".join(sorted(str(x) for x in brands if x))
            meta.append(f"品牌:{brands}")
        if it.get("product_name"):
            meta.append(f'品名:{it["product_name"]}')
        out.append(f'[{n}] {" | ".join(meta) or "无可用元数据"} | {_evidence_text(it)}')
    return "\n".join(out)


def _uniq(seq: Sequence[Any]) -> list:
    seen, out = set(), []
    for x in seq:
        if x and x not in seen:
            seen.add(x); out.append(x)
    return out


# ---------------------------------------------------------------- Stage 2
def write_prototype(items: list[dict], members: Sequence[int], mode_name: str,
                    opp_type: str, ctx_info: dict, ctx) -> dict:
    idx = list(members)
    unit_by_type = {"老品迭代": "失效模式", "新品创新": "诉求主题"}
    if opp_type not in unit_by_type:
        raise ValueError(f"Stage2 不支持的机会类型: {opp_type!r}")
    unit = unit_by_type[opp_type]
    context_parts = [f"生命周期 {opp_type}", f"{unit} {mode_name}"]
    for label, key in (("品类", "category"), ("标签", "tag"),
                       ("语义路径", "tax_path"), ("内容通道", "channel")):
        if ctx_info.get(key):
            context_parts.append(f"{label} {ctx_info[key]}")
    if ctx_info.get("low_star_rate") is not None:
        context_parts.append(f'1-2星占比 {ctx_info["low_star_rate"]}')
    interactions = sum(items[i].get("interactions") or 0 for i in idx)
    if interactions:
        context_parts.append(f"互动量合计 {interactions}")
    ctx_line = " | ".join(context_parts)

    prompt = prompts.STAGE2.format(
        unit_name=unit, actions=" ".join(prompts.ACTIONS),
        banned="、".join(prompts.BANNED_WORDS), context_line=ctx_line,
        n=len(idx), countries=",".join(_uniq(items[i].get("country") for i in idx))[:60] or "-",
        product_names=",".join(_uniq(items[i].get("product_name") for i in idx))[:80] or "-",
        items=_fmt_items(items, idx), opp_type=opp_type)

    last_err: list[str] = []
    last_obj: dict | None = None
    last_meta: dict = {}
    for _ in range(3):                            # 首次 + 2 次带约束重试
        p = prompt if not last_err else (
            prompt + "\n\n【上次输出被校验打回，请修正后重新输出】\n- " + "\n- ".join(last_err))
        try:
            obj, meta = llm.chat_json(p, max_tokens=1800,
                                      required=["title", "desc_phenomenon", "desc_attribution"])
            ctx.bump(tokens=meta.get("tokens", 0))
        except llm.LLMError as e:
            # 这三轮只给「调用成功但业务校验不通过」使用。
            # transport 层已经完成自己的瞬时错误重试；耗尽后必须
            # 直接失败，不能再被外层当成三次「校验纠错」。
            ctx.bump(failed=1)
            raise
        last_obj, last_meta = obj, meta
        errs = validate.validate_stage2(obj, items, idx)
        if not errs:
            obj["_meta"] = meta
            obj["_needs_review"] = False
            return obj
        last_err = errs

    # 三次仍不过：落库并标记 needs_review，不阻断整批（§5.9）
    if last_obj is None:
        ctx.bump(failed=1)
        raise llm.LLMError("Stage2 三次尝试均未取得可解析响应")
    last_obj["_meta"] = last_meta
    last_obj["_needs_review"] = True
    last_obj["_errors"] = last_err
    return last_obj


# ---------------------------------------------------------------- Stage 3
def write_suggestion(title: str, phenomenon: str, attribution: str,
                     history: list[str], ctx) -> tuple[str, str]:
    """返回 (建议段, ctx_hash)。历史上下文固定取 20 条并哈希，保证可复现。"""
    ctx_items = sorted(history)[:20]
    ctx_hash = hashlib.sha256("|".join(ctx_items).encode()).hexdigest()[:16]
    prompt = prompts.STAGE3.format(
        title=title, phenomenon=phenomenon, attribution=attribution,
        ctx_n=len(ctx_items),
        context="\n".join(f"- {x}" for x in ctx_items) or "（暂无历史机会点）")
    last_err: list[str] = []
    for _ in range(3):
        p = prompt if not last_err else prompt + "\n\n【上次被打回】\n- " + "\n- ".join(last_err)
        try:
            obj, meta = llm.chat_json(p, max_tokens=600, required=["desc_suggestion"])
            ctx.bump(tokens=meta.get("tokens", 0))
        except llm.LLMError as e:
            ctx.bump(failed=1)
            raise
        text = (obj.get("desc_suggestion") or "").strip()
        errs = validate.check_suggestion(text)
        if not errs:
            return text, ctx_hash
        last_err = errs
    ctx.bump(failed=1)
    detail = "; ".join(last_err)[:300] or "未返回有效建议"
    raise llm.LLMError(f"Stage3 三次尝试均失败: {detail}")


# ---------------------------------------------------------------- Stage 4
# LLM-as-judge 的已知偏差：「找出问题」这类开放式任务天然倾向产出非空结果。
# 实测即便 Prompt 明确写了「聚合/否定/归纳陈述不算问题」，模型仍会把
# 「另有 26 条类似反馈」「9 条证据全部指向…」列为 issue。Prompt 约束不住，
# 就在代码里兜——这些模式是 Stage2 Prompt 明令要求的规定动作，不是幻觉。
_SOFT_ISSUE = re.compile(
    r"另有\s*\d+\s*条|另有(用户|证据|\d)|"                       # 聚合计数
    r"(全部|所有|均|无一|皆)\s*\d*\s*条?(证据|反馈|评价)?\s*(均|都|全部)?\s*"
    r"(未|没有|不)(提及|涉及|包含|说明)|"                             # 否定陈述
    r"(全部|所有|均|三条|多条|数条)\s*(证据|反馈)?\s*(均|都)?\s*(指向|来自|属于|涉及)"  # 归纳总结
)


def _filter_soft(issues: list[dict]) -> tuple[list[dict], list[dict]]:
    """把复核 issue 分成【硬问题】与【软噪音】。软噪音留档不触发 needs_review。"""
    hard, soft = [], []
    for i in issues or []:
        (soft if _SOFT_ISSUE.search(str(i.get("text") or "")) else hard).append(i)
    return hard, soft


def llm_review(obj: dict, suggestion: str, items: list[dict],
               members: Sequence[int], opp_type: str, ctx) -> dict:
    """对每条产出都跑一次独立复核（不只在程序化校验失败时）。"""
    if opp_type not in ("老品迭代", "新品创新"):
        raise ValueError(f"Stage4 不支持的机会类型: {opp_type!r}")
    prompt = prompts.STAGE4_REVIEW.format(
        title=obj.get("title", ""), phenomenon=obj.get("desc_phenomenon", ""),
        attribution=obj.get("desc_attribution", ""), suggestion=suggestion,
        opp_type=opp_type, n=len(list(members)), items=_fmt_items(items, list(members)))
    r, meta = llm.chat_json(prompt, max_tokens=2000, required=["ok"])  # 900 会截断 JSON
    ctx.bump(tokens=meta.get("tokens", 0))
    hard, soft = _filter_soft(r.get("issues") or [])
    return {"ok": not hard, "issues": hard, "soft_issues": soft}
