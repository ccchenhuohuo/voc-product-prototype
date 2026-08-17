"""生成与消解编排（PRD v8 §4.3–4.4 / §5 / §6）。"""
from __future__ import annotations
import hashlib, json, re, unicodedata
from collections import Counter, defaultdict
from typing import Sequence

from psycopg.types.json import Jsonb

from . import config as C, db, llm, prompts, resolve
from .stages import generate, stage1, validate


_IDENTITY_SEP = re.compile(r"[\s\-_—–/|，,。.;；:：()（）\[\]【】]+")


def _identity_text(value: str | None) -> str:
    """稳定化身份文本；不引入来源、周次或模型版本。"""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return _IDENTITY_SEP.sub(" ", text).strip()


def opportunity_identity_key(opp_type: str, core_tag: str | None,
                             problem_mode: str | None,
                             channel: str | None = None) -> str:
    """v2 机会身份材料，与生命周期对应的 L1 唯一域保持一致。

    老品 L1 是 ``(opp_type, core_tag)``，不含来源或 channel；新品 L1
    是 ``(opp_type, channel, core_tag)``，所以 channel 必须进入身份域。
    ``channel`` 是业务内容通道，不是 ``src_line`` 数据来源。
    """
    if opp_type not in {"老品迭代", "新品创新"}:
        raise ValueError(f"未知机会类型：{opp_type!r}")
    material = {
        "version": 2,
        "opp_type": opp_type,
        "core_tag": _identity_text(core_tag),
        "problem_mode": _identity_text(problem_mode),
    }
    if opp_type == "新品创新":
        normalized_channel = _identity_text(channel)
        if not normalized_channel:
            raise ValueError("新品创新身份缺少 channel")
        material["channel"] = normalized_channel
    return json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_opp_id(opp_type: str, core_tag: str | None,
                problem_mode: str | None,
                channel: str | None = None) -> str:
    """新建行使用独立命名空间，避免与存量 ``OPP-*`` 发生主键冲突。"""
    material = opportunity_identity_key(
        opp_type, core_tag, problem_mode, channel)
    digest = hashlib.sha256(material.encode()).hexdigest()[:32]
    return f"OPP2-{digest.upper()}"


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
        s = (items[i].get("evidence_text") or items[i].get("snippet")
             or items[i].get("content") or "").strip()
        s = re.sub(r"\s+", " ", s)[:200]
        if s and s not in out:
            out.append(s)
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------- 诉求门
def intent_gate(rows: list[dict], ctx) -> tuple[list[dict], dict]:
    """规则门之后的 LLM 二分类。低置信也放行（R11：宁可多看不可漏）。"""
    RULE = re.compile(
        r"能不能|能否|可不可以|可以出|出个|出一个|出一款|什么时候出|什麼時候|啥时候|"
        r"希望|建议|求个|求一个|多研发|单独(出|做|购买|卖|买)|考虑.*出|意向.*出|"
        r"为什么不能|為什麼不能|设想|可行性|衍生产品|"
        r"is there any (option|plan|way)|will there be|any plan|hope.*(add|make)|"
        r"wish.*(had|would)|could you (add|make)|should (make|add)|improvement would be",
        re.I)
    pre = [r for r in rows
           if RULE.search(r.get("evidence_text") or r.get("content") or "")]
    stats = {"total": len(rows), "rule_pass": len(pre), "intent": defaultdict(int)}

    def classify(r: dict) -> dict:
        obj, meta = llm.chat_json(prompts.INTENT_GATE.format(
            content=(r.get("evidence_text") or r.get("content") or "")[:800],
            platform=r.get("platform"), interactions=r.get("interactions") or 0,
            brands=",".join(r.get("brands") or []) or "-"),
            max_tokens=250, required=["intent"])
        ctx.bump(tokens=meta.get("tokens", 0))
        return {**r, "_intent": obj.get("intent"),
                "_conf": float(obj.get("confidence") or 0)}

    out = []
    for res in llm.parallel_map(classify, pre):
        stats["intent"][res["_intent"]] += 1
        if res["_intent"] == "需求缺口" and res["_conf"] >= C.INTENT_REVIEW:
            res["_needs_review"] = res["_conf"] < C.INTENT_PASS
            out.append(res)
    stats["intent"] = dict(stats["intent"])
    stats["passed"] = len(out)
    ctx.metric_update(("intent_gate",), **stats)
    return out, stats


def _bucket_context(bucket, items: list[dict]) -> dict:
    categories = sorted({r.get("category") for r in items if r.get("category")})
    prod_lines = sorted({r.get("prod_line") for r in items if r.get("prod_line")})
    paths = Counter(r.get("tax_path") for r in items if r.get("tax_path"))
    stars = [r["star"] for r in items if r.get("star") is not None]
    low_rate = (sum(1 for star in stars if star <= 2) / len(stars)) if stars else None
    return {
        "bucket_key": f"{bucket.opp_type}|{bucket.topic}|{bucket.channel or '-'}",
        "category": categories[0] if len(categories) == 1 else (
            "跨品类" if categories else "未定"),
        "storage_category": categories[0] if len(categories) == 1 else None,
        "tag": bucket.topic if bucket.opp_type == "老品迭代" else None,
        "channel": bucket.channel,
        "tax_path": paths.most_common(1)[0][0] if paths else "",
        "prod_line": prod_lines[0] if len(prod_lines) == 1 else (
            "通用" if prod_lines else "未定"),
        "low_star_rate": round(low_rate, 4) if low_rate is not None else None,
    }


def generate_opportunities(week: str, ctx, *, week_start=None, week_end=None,
                           opp_types: set[str] | None = None,
                           limit_buckets: int = 0, limit_rows: int = 0,
                           vote: bool | None = None,
                           verbose: bool = False) -> dict:
    """统一入口：内容池 → 前置分类 → 生命周期分桶 → 生成与持久化。"""
    from .routing import route_evidence_by_lifecycle

    rows = db.generation_pool(week_start, week_end)
    routed = route_evidence_by_lifecycle(rows)
    selected = [(bucket, items) for bucket, items in routed.buckets.items()
                if opp_types is None or bucket.opp_type in opp_types]
    selected.sort(key=lambda pair: (-len(pair[1]), pair[0].opp_type,
                                    pair[0].topic, pair[0].channel or ""))
    available_buckets = len(selected)
    scoped_evidence_rows = sum(len(items) for _, items in selected)
    truncated_bucket_rows = 0
    if limit_buckets:
        truncated_bucket_rows = sum(len(items) for _, items in selected[limit_buckets:])
        selected = selected[:limit_buckets]
    truncated_buckets = available_buckets - len(selected)
    truncated_row_limit = (
        sum(max(len(items) - limit_rows, 0) for _, items in selected)
        if limit_rows else 0
    )
    truncated_rows = truncated_bucket_rows + truncated_row_limit
    selected_evidence_rows = sum(
        min(len(items), limit_rows) if limit_rows else len(items)
        for _, items in selected)

    by_lifecycle = Counter()
    for bucket, items in routed.buckets.items():
        by_lifecycle[bucket.opp_type] += len(items)
    by_source = Counter(r.get("src_line") for r in rows)
    ctx.metric_update(
        ("pool",), evidence_rows=len(rows), routed_rows=sum(len(v) for v in routed.buckets.values()),
        invalid_rows=len(routed.invalid), scoped_evidence_rows=scoped_evidence_rows,
        available_buckets=available_buckets, selected_buckets=len(selected),
        selected_evidence_rows=selected_evidence_rows,
        truncated_buckets=truncated_buckets, truncated_rows=truncated_rows,
        by_lifecycle=dict(sorted(by_lifecycle.items())),
        by_source=dict(sorted(by_source.items())))
    ctx.metric_update(
        ("generation",), planned_buckets=len(selected), completed_buckets=0,
        failed_buckets=0, cancelled_buckets=0, planned_groups=0,
        completed_groups=0, failed_groups=0, cancelled_groups=0,
        planned_persistence=0, completed_persistence=0, failed_persistence=0,
        unclassified_rows=0, dropped_rows=0,
        selected_evidence_rows=selected_evidence_rows,
        intent_gate_input_rows=0, intent_gate_passed_rows=0,
        intent_gate_rejected_rows=0, intent_gate_failed_rows=0,
        scope_truncated_buckets=truncated_buckets,
        scope_truncated_rows=truncated_rows)

    # 限制参数可用来确认「是否会截断」，但生产入口不允许把
    # 子集写入后冒充全量成功。在任何 LLM/持久化前失败，同时把
    # 未启动桶记为 cancelled，run log 可明确看出不完整范围。
    if truncated_buckets or truncated_rows:
        ctx.metric_update(("generation",), cancelled_buckets=len(selected))
        raise RuntimeError(
            "生成范围被限制参数截断："
            f"buckets={truncated_buckets}, rows={truncated_rows}")

    histories = {
        opp_type: [r["problem_mode"] for r in db.q(
            "SELECT problem_mode FROM voc_opportunity "
            "WHERE opp_type=%s AND classification_state='确定' "
            "AND merged_into IS NULL AND problem_mode IS NOT NULL "
            "ORDER BY opp_id LIMIT 20", [opp_type])]
        for opp_type in ({b.opp_type for b, _ in selected})
    }

    def prepare(entry) -> dict:
        bucket, original_items = entry
        try:
            items = original_items[:limit_rows] if limit_rows else original_items
            info = _bucket_context(bucket, items)
            if bucket.opp_type == "新品创新" and bucket.channel == "需求缺口":
                gate_input = len(items)
                ctx.metric_incr(("generation",), intent_gate_input_rows=gate_input)
                try:
                    items, _ = intent_gate(items, ctx)
                except BaseException:
                    ctx.metric_incr(
                        ("generation",), intent_gate_failed_rows=gate_input)
                    raise
                ctx.metric_incr(
                    ("generation",), intent_gate_passed_rows=len(items),
                    intent_gate_rejected_rows=gate_input - len(items))
            if not items:
                ctx.metric_incr(("generation",), completed_buckets=1)
                return {"pairs": [], "bucket": bucket}

            split = stage1.split_bucket(items, bucket.opp_type, info, ctx, vote=vote)
            groups = stage1.merge_similar_modes(split["groups"], ctx)
            if bucket.opp_type == "新品创新":
                groups += [
                    {"mode_name": (items[index].get("evidence_text")
                                   or items[index].get("content") or "")[:40],
                     "members": [index]}
                    for index in split["unclassified"]]
            else:
                db.save_unclassified(
                    [(items[index]["message_id"], items[index]["seq"])
                     for index in split["unclassified"]], week, "unclassified")
            db.save_unclassified(
                [(items[index]["message_id"], items[index]["seq"])
                 for index in split["dropped"]], week, "vote_dropped")
            ctx.metric_incr(
                ("generation",), planned_groups=len(groups),
                unclassified_rows=len(split["unclassified"]),
                dropped_rows=len(split["dropped"]))

            def build(group: dict) -> dict | BaseException:
                try:
                    result = build_opportunity(
                        items, group, bucket.opp_type, info, ctx,
                        histories[bucket.opp_type])
                    ctx.metric_incr(("generation",), completed_groups=1)
                    return result
                except llm.FatalLLMError:
                    # 配额/鉴权：继续跑是白费，立刻停整轮。
                    ctx.metric_incr(("generation",), failed_groups=1)
                    raise
                except Exception as error:  # noqa: BLE001
                    # 单条产出失败不该杀死整个生命周期。这里是「一个机会点」
                    # 的边界：Stage2/3/4 的任何质量校验没过（建议段超长、
                    # 复核 JSON 非法、标题公式不符），只作废这一条，记账后继续。
                    #
                    # 2026-08-17 的 2b 连续四轮都死在这类单条校验上：
                    #   Stage1 JSON 截断 -> Stage4 复核 JSON 非法 ->
                    #   Stage3 建议段 340 字超出 40–320 上限
                    # 每次修一个再跑，下一轮暴露下一个。几千条产出里必然有
                    # 个别条目过不了校验，这是概率问题，不是缺陷。
                    ctx.metric_incr(("generation",), failed_groups=1)
                    return error

            built_raw = llm.parallel_map(build, groups)
            built = [obj for obj in built_raw if not isinstance(obj, BaseException)]
            dropped_groups = [obj for obj in built_raw if isinstance(obj, BaseException)]
            if dropped_groups:
                ratio = len(dropped_groups) / max(len(groups), 1)
                # 比例阈值对小样本没有意义：2 条里坏 1 条是 50%，但它只是 1 条。
                # 2026-08-17 实测就栽在这——757 条坏 1 条放行，2 条坏 1 条却把
                # 整条生命周期判死。因此要求同时超过绝对下限才算系统性失败。
                if (len(dropped_groups) > C.GENERATION_MIN_FAILED_ABS
                        and ratio > C.GENERATION_MAX_FAILED_RATIO):
                    ctx.metric_incr(("generation",), failed_buckets=1)
                    raise llm.LLMError(
                        f"桶内机会点失败率过高: {len(dropped_groups)}/{len(groups)} "
                        f"({ratio:.0%} > {C.GENERATION_MAX_FAILED_RATIO:.0%})，"
                        f"首个错误: {dropped_groups[0]}")
                print(f"   [生成] 桶内 {len(dropped_groups)}/{len(groups)} 条产出失败已跳过"
                      f"：{dropped_groups[0]}")
            ctx.metric_incr(("generation",), completed_buckets=1,
                            planned_persistence=len(built))
            return {"pairs": [(obj, items) for obj in built], "bucket": bucket}
        except Exception:
            ctx.metric_incr(("generation",), failed_buckets=1)
            raise

    created: list[str] = []
    try:
        for prepared in llm.parallel_imap(
                prepare, selected, workers=len(selected) or 1):
            pairs = prepared["pairs"]
            ids = persist_opportunities(
                pairs, week, ctx, verbose=verbose, account=True)
            created.extend(ids)
    finally:
        generation = ctx.metrics.get("generation", {})
        ctx.metric_update(
            ("generation",),
            cancelled_buckets=max(
                int(generation.get("planned_buckets", 0))
                - int(generation.get("completed_buckets", 0))
                - int(generation.get("failed_buckets", 0)), 0),
            cancelled_groups=max(
                int(generation.get("planned_groups", 0))
                - int(generation.get("completed_groups", 0))
                - int(generation.get("failed_groups", 0)), 0),
            cancelled_persistence=max(
                int(generation.get("planned_persistence", 0))
                - int(generation.get("completed_persistence", 0))
                - int(generation.get("failed_persistence", 0)), 0))

    reconciliation = generation_reconciliation(ctx)
    if not reconciliation["complete"]:
        raise RuntimeError(f"生成对账失败：{reconciliation}")
    return {"created": created, "pool_rows": len(rows),
            "invalid_rows": len(routed.invalid), "reconciliation": reconciliation}


def generation_reconciliation(ctx) -> dict:
    """把账本压成 run log 可直接判断完整性的闭环计数。"""
    g = dict(ctx.metrics.get("generation", {}))
    bp = int(g.get("planned_buckets", 0))
    bc = int(g.get("completed_buckets", 0))
    bf = int(g.get("failed_buckets", 0))
    bx = int(g.get("cancelled_buckets", 0))
    gp = int(g.get("planned_groups", 0))
    gc = int(g.get("completed_groups", 0))
    gf = int(g.get("failed_groups", 0))
    gx = int(g.get("cancelled_groups", 0))
    pp = int(g.get("planned_persistence", 0))
    pc = int(g.get("completed_persistence", 0))
    pf = int(g.get("failed_persistence", 0))
    px = int(g.get("cancelled_persistence", 0))
    scope_truncated_buckets = int(g.get("scope_truncated_buckets", 0))
    scope_truncated_rows = int(g.get("scope_truncated_rows", 0))
    selected_evidence_rows = int(g.get("selected_evidence_rows", 0))
    gate_input_rows = int(g.get("intent_gate_input_rows", 0))
    gate_passed_rows = int(g.get("intent_gate_passed_rows", 0))
    gate_rejected_rows = int(g.get("intent_gate_rejected_rows", 0))
    gate_failed_rows = int(g.get("intent_gate_failed_rows", 0))

    # Stage1 每个桶各自记账。这里同时汇总「逻辑证据行」和
    # 「批次尝试行」；开启投票时后者是前者的多倍，两者不能混为
    # 一个处理数。失败时这些数也会被顶层 run-log 保存。
    stage1_buckets: list[dict] = []
    for lifecycle_node in ctx.metrics.get("stage1", {}).values():
        if isinstance(lifecycle_node, dict):
            buckets = lifecycle_node.get("buckets", {})
            if isinstance(buckets, dict):
                stage1_buckets.extend(
                    value for value in buckets.values() if isinstance(value, dict))

    def _sum_stage1(key: str) -> int:
        return sum(int(bucket.get(key, 0)) for bucket in stage1_buckets)

    s1_input = _sum_stage1("input_rows")
    s1_accounted = _sum_stage1("accounted_rows")
    sbp = _sum_stage1("planned_batches")
    sbc = _sum_stage1("completed_batches")
    sbf = _sum_stage1("failed_batches")
    sbx = _sum_stage1("cancelled_batches")
    srp = _sum_stage1("planned_batch_rows")
    src = _sum_stage1("completed_batch_rows")
    srf = _sum_stage1("failed_batch_rows")
    srx = _sum_stage1("cancelled_batch_rows")
    stage1_complete = (
        sbp == sbc + sbf + sbx
        and srp == src + srf + srx
        and s1_input == s1_accounted
        and all(bucket.get("status") == "completed" for bucket in stage1_buckets)
        # 同 gf：sbf/srf 是按失败率放行的批次，属设计允许，不计入不完整。
        # 取消（sbx/srx）仍必须为 0——那代表本轮被中断。
        and sbx == srx == 0
    )
    routing_complete = (
        gate_input_rows == gate_passed_rows + gate_rejected_rows
        and gate_failed_rows == 0
        and selected_evidence_rows == s1_input + gate_rejected_rows
    )
    complete = (
        bp == bc + bf + bx
        and gp == gc + gf + gx
        and pp == pc + pf + px
        and stage1_complete
        and routing_complete
        and scope_truncated_buckets == scope_truncated_rows == 0
        # gf（按质量校验跳过的单条产出）是【设计允许】的，不计入不完整：
        # 几千条产出里必然有个别过不了 Stage2/3/4 校验，这是概率问题。
        # 其余仍必须为 0——失败/取消的桶、取消的分组与持久化都代表本轮
        # 被中断，那才是真的不完整。
        # 2026-08-17：原条件把 gf 也要求为 0，与刚加的单条容错直接冲突——
        # 整轮跑完只跳过 3 条也会在终点判不完整，1,900 万 tokens 白烧。
        and bf == bx == gx == pf == px == 0
    )
    result = {
        "complete": complete,
        "planned_buckets": bp, "completed_buckets": bc,
        "failed_buckets": bf, "cancelled_buckets": bx,
        "planned_groups": gp, "completed_groups": gc,
        "failed_groups": gf, "cancelled_groups": gx,
        "planned_persistence": pp, "completed_persistence": pc,
        "failed_persistence": pf, "cancelled_persistence": px,
        "scope_truncated_buckets": scope_truncated_buckets,
        "scope_truncated_rows": scope_truncated_rows,
        "selected_evidence_rows": selected_evidence_rows,
        "intent_gate_input_rows": gate_input_rows,
        "intent_gate_passed_rows": gate_passed_rows,
        "intent_gate_rejected_rows": gate_rejected_rows,
        "intent_gate_failed_rows": gate_failed_rows,
        "stage1_bucket_count": len(stage1_buckets),
        "stage1_input_rows": s1_input,
        "stage1_accounted_rows": s1_accounted,
        "planned_batches": sbp, "completed_batches": sbc,
        "failed_batches": sbf, "cancelled_batches": sbx,
        "planned_batch_rows": srp, "completed_batch_rows": src,
        "failed_batch_rows": srf, "cancelled_batch_rows": srx,
    }
    ctx.metric_update(("reconciliation",), **result)
    return result


def sync_llm_usage(ctx) -> dict:
    usage = llm.usage()
    ctx.set_llm_usage(usage["calls"], usage["tokens"])
    return usage


def validate_lifecycle_sources(row: dict) -> tuple[str, list[str]]:
    """收尾前校验机会点的生命周期与完整来源集。"""
    opp_type = row.get("opp_type")
    if opp_type not in {"老品迭代", "新品创新"}:
        raise ValueError(
            f"{row.get('opp_id')} 缺少可路由的 opp_type：{opp_type!r}")
    raw_sources = row.get("source_lines")
    if (not isinstance(raw_sources, (list, tuple)) or not raw_sources
            or any(not isinstance(source, str) or not source.strip()
                   for source in raw_sources)):
        raise ValueError(
            f"{row.get('opp_id')} 的 source_lines 非法：{raw_sources!r}")
    sources = [source.strip() for source in raw_sources]
    if len(sources) != len(set(sources)):
        raise ValueError(
            f"{row.get('opp_id')} 的 source_lines 含重复来源：{raw_sources!r}")
    return opp_type, sorted(sources)


# ---------------------------------------------------------------- 生成一个机会点
def build_opportunity(items: list[dict], group: dict, opp_type: str, ctx_info: dict,
                      ctx, history: list[str]) -> dict:
    members = group["members"]
    routed_types = {items[i].get("_opp_type") for i in members}
    if routed_types != {opp_type}:
        raise ValueError(f"分组混入其他生命周期：期望 {opp_type}，实得 {routed_types}")
    from .classification import classify_evidence
    classification = classify_evidence(items[i] for i in members)
    if (classification.opp_type != opp_type
            or classification.classification_state != "确定"):
        raise ValueError(
            f"分组完整证据集分类与路由不一致："
            f"routed={opp_type}, classified={classification}")
    # 兜底占位符不能进 prompt——模型会照抄进标题（实测产出过
    # 「摄影配件品类：填补未命名模式（用户自定义功能组合）」）。
    # 命名失败时让 Stage2 自己从证据里概括，比塞一个空洞的名字更好。
    mode_name = group["mode_name"]
    if mode_name == stage1.PLACEHOLDER_MODE:
        mode_name = "（未命名，请自行从证据中概括）"
    obj = generate.write_prototype(
        items, members, mode_name, opp_type, ctx_info, ctx)
    sugg, ctx_hash = generate.write_suggestion(
        obj.get("title", ""), obj.get("desc_phenomenon", ""),
        obj.get("desc_attribution", ""), history, ctx)
    # LLM 复核是「独立视角兜底」，注释见下方——它不决定 needs_review，
    # 只落库供抽查。既然是参考信息，它自己解析失败就更不该阻断整条产出：
    # 2026-08-17 的 2b 第三轮正是被复核返回的非法 JSON（why 字段混了单引号）
    # 杀掉的，此前老品迭代已烧掉 3,405 次调用 / 507 万 tokens。
    try:
        review = generate.llm_review(obj, sugg, items, members, opp_type, ctx)
    except llm.FatalLLMError:
        raise                                   # 配额/鉴权仍须立刻停
    except Exception as error:                  # noqa: BLE001
        ctx.bump(failed=1)
        review = {"ok": True, "issues": [],
                  "soft_issues": [f"复核不可用：{type(error).__name__}: {error}"]}
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
    source_counts = Counter(items[i]["src_line"] for i in members)
    source_lines = sorted(source_counts)
    core_tag = (ctx_info.get("tag") if opp_type == "老品迭代"
                else _no_placeholder(group["mode_name"]))
    problem_mode = _specific_mode(obj, group["mode_name"])
    return {
        "opp_id": make_opp_id(
            opp_type, core_tag, problem_mode, ctx_info.get("channel")),
        "opp_type": classification.opp_type,
        "classification_state": classification.classification_state,
        "classify_rule": classification.classify_rule,
        # src_line 仅为兼容旧消费者的单值代表；新的跨来源组
        # 没有唯一「发现方」，故取排序第一个以保证重跑稳定。
        "src_line": source_lines[0],
        "source_lines": source_lines,
        "evi_by_source": dict(sorted(source_counts.items())),
        "channel": ctx_info.get("channel") if opp_type == "新品创新" else None,
        "prod_line": ctx_info.get("prod_line"),
        "category": ctx_info.get("storage_category"),
        "category_set": cats or None,
        # 兜底占位符不得落库：problem_mode 是向量化字段，写成「未命名模式」会让
        # 该条在 L2 召回里和什么都像，重演吸附器问题。
        "core_tag": core_tag,
        "problem_mode": problem_mode,
        "title": obj.get("title"),
        "desc_phenomenon": obj.get("desc_phenomenon"),
        "desc_attribution": obj.get("desc_attribution"),
        "desc_suggestion": sugg,
        "safety_flag": bool(obj.get("safety_flag")),
        "safety_evidence_ids": safety_ids or None,
        "evi_total": len(members),
        # 兼容一期看板；可扩展统计使用 evi_by_source/source_lines。
        "evi_ec": source_counts.get("电商", 0),
        "evi_social": source_counts.get("社媒", 0),
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
def lock_opportunities(connection, opp_ids: Sequence[str]) -> None:
    """按稳定顺序锁定即将改写证据集的机会点。

    必须在写 ``voc_opp_evidence`` 之前取锁；否则并发挂载可以各自
    基于看不到对方未提交关系的快照回算，后写者会覆盖权威统计。
    """
    ids = sorted(set(opp_ids))
    if not ids:
        return
    connection.execute("""SELECT opp_id FROM voc_opportunity
                           WHERE opp_id = ANY(%s)
                           ORDER BY opp_id
                           FOR UPDATE""", [ids]).fetchall()


def recount(opp_id: str, connection=None) -> None:
    """按已落库的完整证据集重算分类、计数与排序分。

    build 阶段只看得到当前新组；attach 与 cross-line 都会改变最终
    证据集。因此权威分类放在关系落库之后，不使用 ``src_line`` 参数。
    传入 connection 时，读取与回写和证据改挂共用同一事务。
    """
    if connection is None:
        with db.conn() as c:
            recount(opp_id, c)
        return

    # 独立 recount 也与挂载者使用同一把机会点行锁；
    # save / attach / merge 路径在关系变更前已先取得这把锁。
    lock_opportunities(connection, [opp_id])
    rows = connection.execute("""
      SELECT m.src_line, m.spu, m.star,
             p.requires_spu AS source_requires_spu
        FROM voc_opp_evidence oe
        JOIN voc_message m USING (message_id)
        JOIN voc_source_policy p USING (src_line)
       WHERE oe.opp_id=%s
       ORDER BY oe.message_id, oe.seq
    """, [opp_id]).fetchall()
    from .classification import classify_evidence
    classification = classify_evidence(rows)
    source_counts = Counter(row["src_line"] for row in rows)
    source_lines = sorted(source_counts)
    ec = source_counts.get("电商", 0)
    social = source_counts.get("社媒", 0)
    stars = [row["star"] for row in rows if row.get("star") is not None]
    low_star_rate = (sum(1 for star in stars if star <= 2) / len(stars)) if stars else None
    rank_base = len(rows) * (1 + (low_star_rate or 0))
    if len(source_counts) > 1:
        rank_base *= 1.5

    connection.execute("""
      UPDATE voc_opportunity
         SET evi_total=%s,
             evi_ec=%s,
             evi_social=%s,
             evi_by_source=%s,
             source_lines=%s,
             low_star_rate=%s,
             opp_type=%s,
             classification_state=%s,
             classify_rule=%s,
             rank_score=%s + CASE WHEN safety_flag THEN 1000 ELSE 0 END
       WHERE opp_id=%s
    """, [len(rows), ec, social, Jsonb(dict(sorted(source_counts.items()))),
          source_lines or None,
          round(low_star_rate, 4) if low_star_rate is not None else None,
          classification.opp_type, classification.classification_state,
          classification.classify_rule, rank_base, opp_id])


def save_opportunity(opp: dict, items: list[dict], week: str, ctx) -> str:
    """消解（新建 or 挂到已有条目）后落库，返回最终 opp_id。"""
    members = opp.pop("_members")
    opp.pop("_review", None)
    # 注意：不能 pop mode_vec —— 它必须随 opp 一起入库，否则 L2 召回与
    # 跨线汇聚（§6.5）全部失效。早期这里 pop 掉了，实测 mode_vec 0/40。
    dec = (resolve.resolve_one(opp, ctx) if opp.get("mode_vec") is not None
           else {"action": "create", "opp_id": None, "proposals": []})

    llm.ensure_available()
    with db.conn() as c:
        action = dec["action"]
        if action == "attach":
            opp_id = dec["opp_id"]
            # 已有条目：只追加证据与统计，语义字段由触发器按锁级别决定是否放行
            c.execute("UPDATE voc_opportunity SET last_week=%s, updated_at=now() "
                      "WHERE opp_id=%s", [week, opp_id])
        else:
            opp_id = opp["opp_id"]
            # 同一 v2 身份的并发 create 串行化；存量 OPP-* 不在此命名空间。
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                      [opp_id])
            existing = c.execute(
                "SELECT opp_id, opp_type, core_tag, problem_mode, channel, "
                "classification_state, merged_into, safety_flag "
                "FROM voc_opportunity "
                "WHERE opp_id=%s FOR UPDATE", [opp_id]).fetchone()
            if existing:
                if existing.get("merged_into"):
                    # 普通 MERGE 源被清空证据后会 recount 成 R0，opp_type
                    # 合法地变为 NULL；不能拿墓碑当前类型重算原 ID。身份内容
                    # 字段仍保留，先用它们（含新品业务 channel）排除碰撞。
                    expected_content = (
                        _identity_text(opp.get("core_tag")),
                        _identity_text(opp.get("problem_mode")),
                        (_identity_text(opp.get("channel"))
                         if opp["opp_type"] == "新品创新" else ""),
                    )
                    actual_content = (
                        _identity_text(existing.get("core_tag")),
                        _identity_text(existing.get("problem_mode")),
                        _identity_text(existing.get("channel")),
                    )
                    if actual_content != expected_content:
                        raise RuntimeError(f"opp_id 墓碑身份冲突：{opp_id}")
                else:
                    expected = opportunity_identity_key(
                        opp["opp_type"], opp.get("core_tag"),
                        opp.get("problem_mode"), opp.get("channel"))
                    actual = opportunity_identity_key(
                        existing["opp_type"], existing.get("core_tag"),
                        existing.get("problem_mode"), existing.get("channel"))
                    if actual != expected:
                        raise RuntimeError(f"opp_id 哈希冲突：{opp_id}")

                # exact ID 可能已经被人工 MERGE 成墓碑。不得把新证据重新
                # 挂回隐藏源行；沿 merged_into 链锁定最终 canonical。循环或
                # 悬空引用代表血缘损坏，必须失败而不是猜一个目标。
                canonical = existing
                seen = {canonical["opp_id"]}
                while canonical.get("merged_into"):
                    next_id = canonical["merged_into"]
                    if next_id in seen:
                        raise RuntimeError(f"merged_into 出现循环：{sorted(seen | {next_id})}")
                    seen.add(next_id)
                    canonical = c.execute(
                        "SELECT opp_id, opp_type, core_tag, problem_mode, channel, "
                        "classification_state, merged_into, safety_flag "
                        "FROM voc_opportunity WHERE opp_id=%s FOR UPDATE",
                        [next_id]).fetchone()
                    if canonical is None:
                        raise RuntimeError(
                            f"merged_into 指向不存在的机会点：{next_id}")
                    if len(seen) > 20:
                        raise RuntimeError(f"merged_into 链过深：{opp_id}")

                if (canonical["classification_state"] != "确定"
                        or canonical["opp_type"] != opp["opp_type"]):
                    raise RuntimeError(
                        f"exact ID 的 canonical 不可挂载：{canonical['opp_id']}")
                # resolve 对安全候选禁止自动合并；只要已落库 canonical 是安全类，
                # exact-ID 兜底就不能把 create 静默改回 attach。当前没有安全的
                # 第二个稳定 ID 命名空间，因此选择失败即停，保留人工保护。
                if canonical.get("safety_flag"):
                    raise RuntimeError(
                        f"安全机会禁止 exact-ID 自动挂载：{canonical['opp_id']}")
                opp_id = canonical["opp_id"]
                action = "attach"
                c.execute("UPDATE voc_opportunity SET last_week=%s, updated_at=now() "
                          "WHERE opp_id=%s", [week, opp_id])
            else:
                opp["first_week"] = opp["last_week"] = week
                opp["backlog"] = True
                db.upsert_in_transaction(
                    c, "voc_opportunity",
                    [{k: v for k, v in opp.items() if not k.startswith("_")}],
                    ["opp_id"])

        db.upsert_in_transaction(
            c, "voc_opp_evidence",
            [{"opp_id": opp_id, "message_id": items[i]["message_id"],
              "seq": items[i].get("seq", 0), "attach_week": week,
              "match_by": "rule" if action == "create" else "llm",
              "confidence": round(dec.get("confidence", 1.0), 3)} for i in members],
            ["opp_id", "message_id", "seq"])

        for p in dec.get("proposals", []):
            # 安全类 same 判定按策略仍新建一条并交人工决定。旧 resolver 在
            # 新 ID 尚未产生时只能返回候选 ID；落库后补上本条，避免生成一个
            # 只有单个 ID、执行器必然跳过的 MERGE 提案。
            if p.get("op_type") == "MERGE":
                proposal_ids = list(dict.fromkeys(p.get("opp_ids", [])))
                if opp_id not in proposal_ids:
                    proposal_ids.append(opp_id)
                # 自合并提案不可执行且没有人工决策意义。
                if len(proposal_ids) < 2:
                    continue
                p["opp_ids"] = proposal_ids
            c.execute("INSERT INTO voc_proposal(op_type,opp_ids,rationale,week) "
                      "VALUES(%s,%s,%s,%s)",
                      [p["op_type"], p["opp_ids"], p["rationale"], week])

        # 机会点、关系与权威回算不可分割；任一步失败整体回滚。
        recount(opp_id, c)
        # 事务提交前最后检查跨进程取消；fatal 若在 DB 操作期间到达，
        # 这里抛出会让连接上下文回滚，而不是在全局失败后继续提交。
        llm.ensure_available()
    return opp_id


def attach_evidence(opp_id: str, rows: list[dict]) -> int:
    """cross-source 证据挂载与完整集合回算共用一个事务。"""
    if not rows:
        return 0
    with db.conn() as c:
        # 先锁机会点、再写外键关系，避免并发挂载快照丢失。
        lock_opportunities(c, [opp_id])
        n = db.upsert_in_transaction(
            c, "voc_opp_evidence", rows, ["opp_id", "message_id", "seq"])
        recount(opp_id, c)
    return n


def persist_opportunities(pairs: list, week: str, ctx, verbose: bool = False,
                          account: bool = False) -> list[str]:
    """批量向量化后落库。早期是逐条 llm.embed()，每条一次 HTTP 往返；
    改为按 EMBED_BATCH 批量（实测上限 10），调用次数降一个数量级。"""
    if not pairs:
        return []
    try:
        llm.ensure_available()
        vecs = llm.embed([o["problem_mode"] for o, _ in pairs])
        llm.ensure_available()
        if len(vecs) != len(pairs):
            raise llm.LLMError(
                f"Embedding 结果数不匹配: expected={len(pairs)}, actual={len(vecs)}")
    except BaseException:
        if account:
            ctx.metric_incr(("generation",), failed_persistence=len(pairs))
        raise
    out = []
    for (opp, items), vec in zip(pairs, vecs):
        llm.ensure_available()
        opp["mode_vec"] = vec
        title = opp.get("title") or ""
        try:
            oid = save_opportunity(opp, items, week, ctx)
        except BaseException:
            if account:
                ctx.metric_incr(("generation",), failed_persistence=1)
            raise
        out.append(oid)
        if account:
            ctx.metric_incr(("generation",), completed_persistence=1)
        if verbose:
            print(f"      -> {oid}  {title[:46]}", flush=True)
    return out
