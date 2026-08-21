"""生成与消解编排（PRD v8 §4.3–4.4 / §5 / §6）。"""
from __future__ import annotations
import hashlib, json, re, unicodedata
from collections import Counter
from typing import Iterable, Mapping, Sequence

from psycopg.types.json import Jsonb

from . import config as C, db, llm, prompts, resolve
from .stages import generate, precluster, stage1, validate, value_gate


_IDENTITY_SEP = re.compile(r"[\s\-_—–/|，,。.;；:：()（）\[\]【】]+")

AssignmentKey = tuple[str, int, str]


def _assignment_key(row: Mapping[str, object]) -> AssignmentKey:
    """从快照、路由、关系或终态行提取同一份扇出键。"""
    message_id = row.get("message_id")
    seq = row.get("seq", 0)
    assigned_spu = row.get("assigned_spu")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError(f"归属键缺少 message_id：{message_id!r}")
    if not isinstance(seq, int) or isinstance(seq, bool):
        raise ValueError(f"归属键 seq 必须是整数：{seq!r}")
    if (not isinstance(assigned_spu, str) or not assigned_spu
            or assigned_spu != assigned_spu.strip()):
        raise ValueError(f"归属键 assigned_spu 非法：{assigned_spu!r}")
    return message_id, seq, assigned_spu


def _sample_assignment_keys(keys: Iterable[AssignmentKey]) -> list[str]:
    """错误与 run log 共用的稳定样例；最多 10 个，避免巨量日志。"""
    return [repr(key) for key in sorted(set(keys))[:10]]


def assignment_route_reconciliation(
    snapshot_rows: Iterable[Mapping[str, object]],
    routed_rows: Iterable[Mapping[str, object]],
) -> dict:
    """对物理快照与实际路由行同时做计数和双向键差集。"""
    snapshot_keys = [_assignment_key(row) for row in snapshot_rows]
    routed_keys = [_assignment_key(row) for row in routed_rows]
    snapshot_set = set(snapshot_keys)
    routed_set = set(routed_keys)
    snapshot_only = snapshot_set - routed_set
    routed_only = routed_set - snapshot_set
    snapshot_duplicates = len(snapshot_keys) - len(snapshot_set)
    routed_duplicates = len(routed_keys) - len(routed_set)
    complete = (
        not snapshot_only
        and not routed_only
        and snapshot_duplicates == 0
        and routed_duplicates == 0
        and len(snapshot_keys) == len(routed_keys)
    )
    return {
        "complete": complete,
        "snapshot_rows": len(snapshot_keys),
        "snapshot_distinct_rows": len(snapshot_set),
        "routed_rows": len(routed_keys),
        "routed_distinct_rows": len(routed_set),
        "snapshot_duplicate_rows": snapshot_duplicates,
        "routed_duplicate_rows": routed_duplicates,
        "snapshot_except_routed_rows": len(snapshot_only),
        "routed_except_snapshot_rows": len(routed_only),
        "snapshot_except_routed_samples": _sample_assignment_keys(snapshot_only),
        "routed_except_snapshot_samples": _sample_assignment_keys(routed_only),
    }


def _assignment_route_error(stat: Mapping[str, object]) -> str:
    return (
        "归属路由守恒失败："
        f"F={stat['snapshot_rows']}, R={stat['routed_rows']}; "
        "snapshot EXCEPT routed="
        f"{stat['snapshot_except_routed_rows']} "
        f"samples={stat['snapshot_except_routed_samples']}; "
        "routed EXCEPT snapshot="
        f"{stat['routed_except_snapshot_rows']} "
        f"samples={stat['routed_except_snapshot_samples']}; "
        f"snapshot_duplicates={stat['snapshot_duplicate_rows']}, "
        f"routed_duplicates={stat['routed_duplicate_rows']}"
    )


def assignment_projection_reconciliation(
    snapshot_rows: Iterable[Mapping[str, object]],
    relation_rows: Iterable[Mapping[str, object]],
    terminal_rows: Iterable[Mapping[str, object]],
    *,
    max_terminal_loss_ratio: float,
) -> dict:
    """计算终态等式及投影双向差集；输入是物理键行而非手工 metrics。"""
    if not 0.0 <= max_terminal_loss_ratio <= 1.0:
        raise ValueError(
            "终态损耗率阈值必须位于 [0, 1]："
            f"{max_terminal_loss_ratio!r}")

    snapshot_keys = [_assignment_key(row) for row in snapshot_rows]
    relation_keys = {_assignment_key(row) for row in relation_rows}
    terminal_keys = {_assignment_key(row) for row in terminal_rows}
    snapshot_set = set(snapshot_keys)

    snapshot_except_relation = snapshot_set - relation_keys
    relation_except_snapshot = relation_keys - snapshot_set
    unaccounted = snapshot_set - relation_keys - terminal_keys
    terminal_except_snapshot = terminal_keys - snapshot_set
    relation_terminal_overlap = relation_keys & terminal_keys

    fanout_rows = len(snapshot_keys)
    projected_unique_rows = len(relation_keys)
    terminal_distinct_rows = len(terminal_keys)
    terminal_loss_ratio = (
        terminal_distinct_rows / fanout_rows if fanout_rows else 0.0
    )
    equation_complete = (
        fanout_rows == projected_unique_rows + terminal_distinct_rows
    )
    complete = (
        len(snapshot_set) == fanout_rows
        and not relation_except_snapshot
        and not unaccounted
        and not terminal_except_snapshot
        and not relation_terminal_overlap
        and equation_complete
        and terminal_loss_ratio <= max_terminal_loss_ratio
    )
    return {
        "complete": complete,
        "snapshot_rows": fanout_rows,
        "snapshot_distinct_rows": len(snapshot_set),
        "projected_distinct_rows": projected_unique_rows,
        "terminal_distinct_rows": terminal_distinct_rows,
        "equation_complete": equation_complete,
        "terminal_loss_ratio": terminal_loss_ratio,
        "max_terminal_loss_ratio": max_terminal_loss_ratio,
        "snapshot_except_relation_rows": len(snapshot_except_relation),
        "relation_except_snapshot_rows": len(relation_except_snapshot),
        "unaccounted_rows": len(unaccounted),
        "terminal_except_snapshot_rows": len(terminal_except_snapshot),
        "relation_terminal_overlap_rows": len(relation_terminal_overlap),
        "snapshot_except_relation_samples": _sample_assignment_keys(
            snapshot_except_relation),
        "relation_except_snapshot_samples": _sample_assignment_keys(
            relation_except_snapshot),
        "unaccounted_samples": _sample_assignment_keys(unaccounted),
        "terminal_except_snapshot_samples": _sample_assignment_keys(
            terminal_except_snapshot),
        "relation_terminal_overlap_samples": _sample_assignment_keys(
            relation_terminal_overlap),
    }


def _assignment_projection_error(stat: Mapping[str, object]) -> str:
    return (
        "v3 终态守恒自检失败："
        f"F={stat['snapshot_rows']}, "
        f"P_unique={stat['projected_distinct_rows']}, "
        f"D={stat['terminal_distinct_rows']}, "
        f"D/F={stat['terminal_loss_ratio']:.4f} "
        f"(上限 {stat['max_terminal_loss_ratio']:.4f}); "
        "snapshot EXCEPT relation="
        f"{stat['snapshot_except_relation_rows']} "
        f"samples={stat['snapshot_except_relation_samples']}; "
        "relation EXCEPT snapshot="
        f"{stat['relation_except_snapshot_rows']} "
        f"samples={stat['relation_except_snapshot_samples']}; "
        "既不在关系也不在终态="
        f"{stat['unaccounted_rows']} samples={stat['unaccounted_samples']}; "
        "terminal EXCEPT snapshot="
        f"{stat['terminal_except_snapshot_rows']} "
        f"samples={stat['terminal_except_snapshot_samples']}; "
        "relation INTERSECT terminal="
        f"{stat['relation_terminal_overlap_rows']} "
        f"samples={stat['relation_terminal_overlap_samples']}"
    )


def _identity_text(value: str | None) -> str:
    """稳定化身份文本；不引入来源、周次或模型版本。"""
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return _IDENTITY_SEP.sub(" ", text).strip()


def opportunity_identity_key(opp_type: str, core_tag: str | None,
                             problem_mode: str | None) -> str:
    """v3 机会身份统一为生命周期、核心标签与问题模式。"""
    if opp_type not in {"老品迭代", "新品创新"}:
        raise ValueError(f"未知机会类型：{opp_type!r}")
    material = {
        "version": 3,
        "opp_type": opp_type,
        "core_tag": _identity_text(core_tag),
        "problem_mode": _identity_text(problem_mode),
    }
    return json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def make_opp_id(opp_type: str, core_tag: str | None,
                problem_mode: str | None) -> str:
    """新建行使用独立命名空间，避免与存量 ``OPP-*`` 发生主键冲突。"""
    material = opportunity_identity_key(opp_type, core_tag, problem_mode)
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


def _bucket_context(bucket, items: list[dict]) -> dict:
    categories = sorted({r.get("category") for r in items if r.get("category")})
    prod_lines = sorted({r.get("prod_line") for r in items if r.get("prod_line")})
    paths = Counter(r.get("tax_path") for r in items if r.get("tax_path"))
    stars = [r["star"] for r in items if r.get("star") is not None]
    low_rate = (sum(1 for star in stars if star <= 2) / len(stars)) if stars else None
    return {
        "bucket_key": f"{bucket.opp_type}|{bucket.topic}",
        "category": categories[0] if len(categories) == 1 else (
            "跨品类" if categories else "未定"),
        "storage_category": categories[0] if len(categories) == 1 else None,
        "tag": bucket.topic if bucket.opp_type == "老品迭代" else None,
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
    """统一入口：G1--G4 → G5 2×2 → 生命周期分桶 → 生成。"""
    from .routing import (
        classify_evidence_by_lifecycle,
        route_classified_evidence,
        route_social_value_evidence,
    )

    ctx.metric_update(
        ("generation",), planned_buckets=0, completed_buckets=0,
        failed_buckets=0, cancelled_buckets=0, planned_groups=0,
        completed_groups=0, failed_groups=0, cancelled_groups=0,
        planned_persistence=0, completed_persistence=0, failed_persistence=0,
        unclassified_rows=0, dropped_rows=0,
        grounding_checked_attempts=0, grounding_enforced_attempts=0,
        grounding_flagged_attempts=0, grounding_issue_count=0,
        grounding_orphan_issues=0, grounding_polarity_issues=0,
        grounding_title_subject_issues=0, grounding_short_evidence_issues=0,
        grounding_rejected_groups=0,
        selected_evidence_rows=0,
        social_candidate_rows=0, g1_competitor_rows=0,
        g2_dewater_rows=0, g3_official_rows=0,
        structural_passed_rows=0, value_gate_input_rows=0,
        value_gate_input_messages=0, value_gate_cache_hit_messages=0,
        value_gate_llm_messages=0, value_gate_llm_votes=0,
        value_gate_failed_rows=0, g4_no_value_rows=0,
        generic_claim_rows=0, unassigned_defect_rows=0,
        social_old_pool_rows=0, social_innovation_pool_rows=0,
        precluster_enabled=C.PRECLUSTER_ENABLED,
        precluster_target_rows=0,
        precluster_input_rows=0, precluster_input_units=0,
        precluster_embedded_units=0, precluster_embed_failed_units=0,
        precluster_clusters=0, precluster_singleton_clusters=0,
        precluster_oversized_clusters=0, precluster_claim_empty=0,
        precluster_cluster_member_units=0, precluster_cluster_member_rows=0,
        precluster_planned_buckets=0,
        assignment_snapshot_rows=0, assignment_unique_facts=0,
        assignment_expected_rows=0, assignment_routed_rows=0,
        assignment_conservation=False,
        scope_truncated_buckets=0, scope_truncated_rows=0)

    structural = db.social_structural_gate_counts(week_start, week_end)
    snapshot_rows = db.verify_assign_snapshot(ctx.run_id)
    snapshot_assignment_rows = db.load_assign_snapshot_rows(ctx.run_id)
    if len(snapshot_assignment_rows) != snapshot_rows:
        raise RuntimeError(
            "归属快照物理读取行数与指纹校验不一致："
            f"verified={snapshot_rows}, loaded={len(snapshot_assignment_rows)}")
    rows = db.generation_pool(
        week_start, week_end, assign_run_id=ctx.run_id)
    ecommerce_rows = [row for row in rows if row.get("src_line") == "电商"]
    social_rows = [row for row in rows if row.get("src_line") == "社媒"]

    ecommerce_classified, invalid = classify_evidence_by_lifecycle(ecommerce_rows)
    try:
        gate = value_gate.apply_value_gate(social_rows, ctx)
    except BaseException:
        ctx.metric_update(
            ("generation",), value_gate_input_rows=len(social_rows),
            value_gate_failed_rows=len(social_rows))
        raise
    social_routing = route_social_value_evidence(gate.passed_rows)
    classified = ecommerce_classified + social_routing.eligible

    social_by_lifecycle = Counter(
        row["_opp_type"] for row in social_routing.eligible
    )
    ctx.metric_update(
        ("generation",),
        social_candidate_rows=int(structural.get("social_candidate_rows", 0)),
        g1_competitor_rows=int(structural.get("g1_competitor_rows", 0)),
        g2_dewater_rows=int(structural.get("g2_dewater_rows", 0)),
        g3_official_rows=int(structural.get("g3_official_rows", 0)),
        structural_passed_rows=int(structural.get("structural_passed_rows", 0)),
        value_gate_input_rows=len(social_rows),
        value_gate_input_messages=int(gate.stats["input_messages"]),
        value_gate_cache_hit_messages=int(gate.stats["cache_hit_messages"]),
        value_gate_llm_messages=int(gate.stats["llm_messages"]),
        value_gate_llm_votes=int(gate.stats["llm_votes"]),
        value_gate_failed_rows=len(gate.failed_rows),
        g4_no_value_rows=len(gate.no_value_rows),
        generic_claim_rows=len(gate.generic_claim_rows),
        unassigned_defect_rows=len(social_routing.unassigned_defects),
        social_old_pool_rows=social_by_lifecycle.get("老品迭代", 0),
        social_innovation_pool_rows=social_by_lifecycle.get("新品创新", 0),
    )
    if gate.failed_rows:
        raise llm.LLMError(
            f"G4 单消息判定失败，涉及 {len(gate.failed_rows)} 条证据")

    scoped_rows = [
        row for row in classified
        if opp_types is None or row.get("_opp_type") in opp_types
    ]
    if C.PRECLUSTER_ENABLED:
        innovation_rows = [
            row for row in scoped_rows if row.get("_opp_type") == "新品创新"
        ]
        non_innovation_rows = [
            row for row in scoped_rows if row.get("_opp_type") != "新品创新"
        ]
        ctx.metric_update(
            ("generation",), precluster_target_rows=len(innovation_rows))
        clustered = precluster.cluster_claims(innovation_rows, ctx)
        pre_stats = clustered["stats"]
        ctx.metric_update(
            ("generation",),
            precluster_input_rows=pre_stats["input_rows"],
            precluster_input_units=pre_stats["input_units"],
            precluster_embedded_units=pre_stats["embedded_units"],
            precluster_embed_failed_units=pre_stats["embed_failed_units"],
            precluster_clusters=pre_stats["clusters"],
            precluster_singleton_clusters=pre_stats["singleton_clusters"],
            precluster_oversized_clusters=pre_stats["oversized_clusters"],
            precluster_claim_empty=pre_stats["claim_empty"],
            precluster_cluster_member_units=pre_stats["cluster_member_units"],
            precluster_cluster_member_rows=pre_stats["cluster_member_rows"],
        )
        if pre_stats["claim_empty"]:
            raise RuntimeError(
                "G4 已应将空/占位 claim 收敛为「诉求过泛」，"
                f"预聚类仍收到 {pre_stats['claim_empty']} 条空 claim")
        clustered_rows = [
            row
            for cluster_rows in clustered["clusters"].values()
            for row in cluster_rows
        ]
        routed = route_classified_evidence(non_innovation_rows + clustered_rows)
    else:
        ctx.metric_update(
            ("generation",),
            precluster_target_rows=sum(
                row.get("_opp_type") == "新品创新" for row in scoped_rows
            ),
        )
        routed = route_classified_evidence(scoped_rows)

    routed_old_items = [
        row
        for bucket, items in routed.buckets.items()
        if bucket.opp_type == "老品迭代"
        for row in items
    ]
    if any(
        not row.get("assigned_spu")
        or row.get("assignment_source") not in {"fact", "root"}
        or row.get("assign_run_id") != ctx.run_id
        for row in routed_old_items
    ):
        raise RuntimeError("老品路由行缺少当前 run_id 的完整归属投影")

    old_scope_enabled = opp_types is None or "老品迭代" in opp_types
    if old_scope_enabled:
        assignment_route = assignment_route_reconciliation(
            snapshot_assignment_rows, routed_old_items)
        ctx.metric_update(
            ("generation", "assignment_route"), **assignment_route)
        if not assignment_route["complete"]:
            raise RuntimeError(_assignment_route_error(assignment_route))
        assignment_fanout = int(assignment_route["snapshot_rows"])
        routed_old_rows = int(assignment_route["routed_rows"])
        assignment_scope = "checked"
    else:
        # rerun_both 的新品进程与老品进程共用 run_id；快照只定义老品 F，
        # 新品进程不声称证明它，真正的检查由老品进程完成。
        assignment_fanout = 0
        routed_old_rows = 0
        assignment_scope = "not-requested"
    assignment_unique_facts = len({
        (row["message_id"], row["seq"])
        for row in snapshot_assignment_rows
    })
    ctx.metric_update(
        ("generation",),
        assignment_snapshot_rows=snapshot_rows,
        assignment_unique_facts=assignment_unique_facts,
        assignment_expected_rows=assignment_fanout,
        assignment_routed_rows=routed_old_rows,
        assignment_conservation_scope=assignment_scope,
        assignment_conservation=True,
    )

    if old_scope_enabled:
        cleared_terminal_rows = db.clear_terminal_evidence(ctx.run_id)
        ctx.metric_update(
            ("generation",), terminal_ledger_reset_rows=cleared_terminal_rows)

    selected = list(routed.buckets.items())
    selected.sort(key=lambda pair: (-len(pair[1]), pair[0].opp_type,
                                    pair[0].topic))
    available_buckets = len(selected)
    scoped_evidence_rows = len(scoped_rows)
    truncated_bucket_rows = 0
    truncated_items: list[dict] = []
    if limit_buckets:
        truncated_items.extend(
            row for _, items in selected[limit_buckets:] for row in items)
        truncated_bucket_rows = sum(len(items) for _, items in selected[limit_buckets:])
        selected = selected[:limit_buckets]
    truncated_buckets = available_buckets - len(selected)
    truncated_row_limit = (
        sum(max(len(items) - limit_rows, 0) for _, items in selected)
        if limit_rows else 0
    )
    if limit_rows:
        truncated_items.extend(
            row for _, items in selected for row in items[limit_rows:])
    truncated_rows = truncated_bucket_rows + truncated_row_limit
    selected_stage1_rows = sum(
        min(len(items), limit_rows) if limit_rows else len(items)
        for _, items in selected)
    selected_evidence_rows = selected_stage1_rows

    by_lifecycle = Counter()
    for row in classified:
        by_lifecycle[row["_opp_type"]] += 1
    by_source = Counter(r.get("src_line") for r in rows)
    ctx.metric_update(
        ("pool",), evidence_rows=len(rows), routed_rows=len(classified),
        invalid_rows=len(invalid), scoped_evidence_rows=scoped_evidence_rows,
        available_buckets=available_buckets, selected_buckets=len(selected),
        selected_evidence_rows=selected_evidence_rows,
        truncated_buckets=truncated_buckets, truncated_rows=truncated_rows,
        by_lifecycle=dict(sorted(by_lifecycle.items())),
        by_source=dict(sorted(by_source.items())))
    innovation_planned_buckets = sum(
        bucket.opp_type == "新品创新" for bucket, _ in selected)
    ctx.metric_update(
        ("generation",), planned_buckets=len(selected),
        precluster_planned_buckets=(
            innovation_planned_buckets if C.PRECLUSTER_ENABLED else 0),
        selected_evidence_rows=selected_evidence_rows,
        scope_truncated_buckets=truncated_buckets,
        scope_truncated_rows=truncated_rows)

    # 限制参数可用来确认「是否会截断」，但生产入口不允许把
    # 子集写入后冒充全量成功。语义簇本身决定桶数，因此必须在诉求门与
    # 预聚类后才能判定截断；未启动的老品扇出键先进入显式终态账，随后
    # 整轮仍失败。同时把未启动桶记为 cancelled，run log 可看出不完整范围。
    if truncated_buckets or truncated_rows:
        truncated_old_items = [
            row for row in truncated_items
            if row.get("_opp_type") == "老品迭代"
        ]
        if truncated_old_items:
            db.save_terminal_evidence(
                truncated_old_items, week, "truncated", ctx.run_id)
        ctx.metric_update(("generation",), cancelled_buckets=len(selected))
        raise RuntimeError(
            "生成范围被限制参数截断："
            f"buckets={truncated_buckets}, rows={truncated_rows}")

    histories = {
        opp_type: [r["problem_mode"] for r in db.q(
            "SELECT problem_mode FROM voc_opportunity "
            "WHERE opp_id LIKE 'OPP2-%%' "
            "AND opp_type=%s AND classification_state='确定' "
            "AND merged_into IS NULL AND problem_mode IS NOT NULL "
            "ORDER BY opp_id LIMIT 20", [opp_type])]
        for opp_type in ({b.opp_type for b, _ in selected})
    }

    def prepare(entry) -> dict:
        bucket, original_items = entry
        try:
            items = original_items[:limit_rows] if limit_rows else original_items
            info = _bucket_context(bucket, items)
            if not items:
                ctx.metric_incr(("generation",), completed_buckets=1)
                return {"ids": [], "bucket": bucket}

            split = stage1.split_bucket(items, bucket.opp_type, info, ctx, vote=vote)
            groups = stage1.merge_similar_modes(split["groups"], ctx)
            if bucket.opp_type == "新品创新":
                groups += [
                    {"mode_name": (items[index].get("evidence_text")
                                   or items[index].get("content") or "")[:40],
                     "members": [index]}
                    for index in split["unclassified"]]
            else:
                db.save_terminal_evidence(
                    [items[index] for index in split["unclassified"]],
                    week, "unclassified", ctx.run_id)
            if split["dropped"]:
                if bucket.opp_type == "老品迭代":
                    db.save_terminal_evidence(
                        [items[index] for index in split["dropped"]],
                        week, "vote_dropped", ctx.run_id)
                else:
                    db.save_unclassified(
                        [(items[index]["message_id"], items[index]["seq"])
                         for index in split["dropped"]],
                        week, "vote_dropped")
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
            if bucket.opp_type == "老品迭代":
                for group, outcome in zip(groups, built_raw):
                    if not isinstance(outcome, BaseException):
                        continue
                    terminal_reason = (
                        "grounding_rejected"
                        if str(outcome).startswith(
                            "Stage2 grounding 三次校验仍失败")
                        else "generation_failed"
                    )
                    db.save_terminal_evidence(
                        [items[index] for index in group["members"]],
                        week, terminal_reason, ctx.run_id)
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
            pairs = [(obj, items) for obj in built]
        except Exception:
            ctx.metric_incr(("generation",), failed_buckets=1)
            raise

        # 落库放进桶自己的线程。安全性来自分区：resolve_one 的 L1 只在同一
        # core_tag 内召回候选（resolve.py:23 `core_tag IS NOT DISTINCT FROM %s`），
        # 而桶键就是 core_tag，所以不同桶的卡永远不可能互为合并候选——
        # 区内串行、区间并行，去重语义一个字不变。
        #
        # 这一步是整轮耗时的大头：2026-08-19 实测 generate_existing 4h44m 里
        # Stage1 全量只占 143 秒，其余几乎全在这个此前串行的循环里。每张卡
        # 平均 3 次 L3 判定、单次实测 4.2 秒（收尾 4,097 次调用 / 5h 反推），
        # 串行叠起来就是几小时；并行后关键路径塌缩到最大桶的卡数 × 3。
        #
        # 必须放在 try 之外：桶已计入 completed_buckets，若落库失败再进
        # except 会同时记 failed_buckets，打破 bp == bc + bf + bx 对账不变量。
        # 落库失败自有 failed_persistence 记账，异常也会经 parallel_imap 上抛。
        ids = persist_opportunities(
            pairs, week, ctx, verbose=verbose, account=True)
        return {"ids": ids, "bucket": bucket}

    created: list[str] = []
    try:
        for prepared in llm.parallel_imap(
                prepare, selected, workers=len(selected) or 1):
            created.extend(prepared["ids"])
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
            "invalid_rows": len(invalid), "reconciliation": reconciliation}


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
    assignment_snapshot_rows = int(g.get("assignment_snapshot_rows", 0))
    assignment_unique_facts = int(g.get("assignment_unique_facts", 0))
    assignment_expected_rows = int(g.get("assignment_expected_rows", 0))
    assignment_routed_rows = int(g.get("assignment_routed_rows", 0))
    assignment_conservation = bool(g.get("assignment_conservation", False))
    social_candidate_rows = int(g.get("social_candidate_rows", 0))
    g1_competitor_rows = int(g.get("g1_competitor_rows", 0))
    g2_dewater_rows = int(g.get("g2_dewater_rows", 0))
    g3_official_rows = int(g.get("g3_official_rows", 0))
    structural_passed_rows = int(g.get("structural_passed_rows", 0))
    value_gate_input_rows = int(g.get("value_gate_input_rows", 0))
    value_gate_input_messages = int(g.get("value_gate_input_messages", 0))
    value_gate_cache_hit_messages = int(g.get("value_gate_cache_hit_messages", 0))
    value_gate_llm_messages = int(g.get("value_gate_llm_messages", 0))
    value_gate_llm_votes = int(g.get("value_gate_llm_votes", 0))
    value_gate_failed_rows = int(g.get("value_gate_failed_rows", 0))
    g4_no_value_rows = int(g.get("g4_no_value_rows", 0))
    generic_claim_rows = int(g.get("generic_claim_rows", 0))
    unassigned_defect_rows = int(g.get("unassigned_defect_rows", 0))
    social_old_pool_rows = int(g.get("social_old_pool_rows", 0))
    social_innovation_pool_rows = int(g.get("social_innovation_pool_rows", 0))
    precluster_enabled = bool(g.get("precluster_enabled", False))
    precluster_target_rows = int(g.get("precluster_target_rows", 0))
    precluster_input_rows = int(g.get("precluster_input_rows", 0))
    precluster_input_units = int(g.get("precluster_input_units", 0))
    precluster_embedded_units = int(g.get("precluster_embedded_units", 0))
    precluster_embed_failed_units = int(
        g.get("precluster_embed_failed_units", 0))
    precluster_clusters = int(g.get("precluster_clusters", 0))
    precluster_singleton_clusters = int(
        g.get("precluster_singleton_clusters", 0))
    precluster_oversized_clusters = int(
        g.get("precluster_oversized_clusters", 0))
    precluster_claim_empty = int(g.get("precluster_claim_empty", 0))
    precluster_cluster_member_units = int(
        g.get("precluster_cluster_member_units", 0))
    precluster_cluster_member_rows = int(
        g.get("precluster_cluster_member_rows", 0))
    precluster_planned_buckets = int(
        g.get("precluster_planned_buckets", 0))
    grounding_checked_attempts = int(g.get("grounding_checked_attempts", 0))
    grounding_enforced_attempts = int(g.get("grounding_enforced_attempts", 0))
    grounding_flagged_attempts = int(g.get("grounding_flagged_attempts", 0))
    grounding_issue_count = int(g.get("grounding_issue_count", 0))
    grounding_orphan_issues = int(g.get("grounding_orphan_issues", 0))
    grounding_polarity_issues = int(g.get("grounding_polarity_issues", 0))
    grounding_title_subject_issues = int(
        g.get("grounding_title_subject_issues", 0))
    grounding_short_evidence_issues = int(
        g.get("grounding_short_evidence_issues", 0))
    grounding_rejected_groups = int(g.get("grounding_rejected_groups", 0))

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
    precluster_failed_ratio = (
        precluster_embed_failed_units / max(precluster_input_units, 1))
    precluster_failure_allowed = (
        precluster_embed_failed_units <= C.GENERATION_MIN_FAILED_ABS
        or precluster_failed_ratio <= C.GENERATION_MAX_FAILED_RATIO
    )
    precluster_complete = (
        not precluster_enabled
        or (
            precluster_claim_empty == 0
            and precluster_target_rows == precluster_input_rows
            and precluster_input_units == (
                precluster_embedded_units + precluster_embed_failed_units)
            and precluster_cluster_member_units == (
                precluster_embedded_units + precluster_embed_failed_units)
            # 簇的「成员数」按去重后聚类单元计；同时单独对账
            # 展开后证据行，避免多证据消息的其余行静默消失。
            and precluster_cluster_member_rows == precluster_input_rows
            and precluster_planned_buckets == precluster_clusters
            and 0 <= precluster_singleton_clusters <= precluster_clusters
            and precluster_failure_allowed
        )
    )
    structural_gate_complete = (
        social_candidate_rows == (
            g1_competitor_rows + g2_dewater_rows + g3_official_rows
            + structural_passed_rows
        )
        and structural_passed_rows == value_gate_input_rows
    )
    social_terminal_complete = (
        value_gate_input_rows == (
            g4_no_value_rows + generic_claim_rows + unassigned_defect_rows
            + social_old_pool_rows + social_innovation_pool_rows
            + value_gate_failed_rows
        )
        and value_gate_failed_rows == 0
        and value_gate_input_messages == (
            value_gate_cache_hit_messages + value_gate_llm_messages)
    )
    routing_complete = (
        selected_evidence_rows == s1_input
        and assignment_conservation
        and assignment_expected_rows == assignment_routed_rows
        and precluster_complete
        and structural_gate_complete
        and social_terminal_complete
    )
    # grounding 作废复用 failed_groups 这个既有终态，不能另开一条会让机会点
    # 静默消失的账。其余计数是报告模式观测量，也做基本单调性校验。
    grounding_complete = (
        0 <= grounding_enforced_attempts <= grounding_checked_attempts
        and 0 <= grounding_flagged_attempts <= grounding_checked_attempts
        and grounding_issue_count >= grounding_flagged_attempts
        and grounding_issue_count == (
            grounding_orphan_issues + grounding_polarity_issues
            + grounding_title_subject_issues + grounding_short_evidence_issues)
        and 0 <= grounding_rejected_groups <= grounding_flagged_attempts
        and grounding_rejected_groups <= gf
    )
    complete = (
        bp == bc + bf + bx
        and gp == gc + gf + gx
        and pp == pc + pf + px
        and stage1_complete
        and routing_complete
        and grounding_complete
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
        "assignment_snapshot_rows": assignment_snapshot_rows,
        "assignment_unique_facts": assignment_unique_facts,
        "assignment_expected_rows": assignment_expected_rows,
        "assignment_routed_rows": assignment_routed_rows,
        "assignment_conservation": assignment_conservation,
        "social_candidate_rows": social_candidate_rows,
        "g1_competitor_rows": g1_competitor_rows,
        "g2_dewater_rows": g2_dewater_rows,
        "g3_official_rows": g3_official_rows,
        "structural_passed_rows": structural_passed_rows,
        "value_gate_input_rows": value_gate_input_rows,
        "value_gate_input_messages": value_gate_input_messages,
        "value_gate_cache_hit_messages": value_gate_cache_hit_messages,
        "value_gate_llm_messages": value_gate_llm_messages,
        "value_gate_llm_votes": value_gate_llm_votes,
        "value_gate_failed_rows": value_gate_failed_rows,
        "g4_no_value_rows": g4_no_value_rows,
        "generic_claim_rows": generic_claim_rows,
        "unassigned_defect_rows": unassigned_defect_rows,
        "social_old_pool_rows": social_old_pool_rows,
        "social_innovation_pool_rows": social_innovation_pool_rows,
        "structural_gate_complete": structural_gate_complete,
        "social_terminal_complete": social_terminal_complete,
        "precluster_enabled": precluster_enabled,
        "precluster_target_rows": precluster_target_rows,
        "precluster_input_rows": precluster_input_rows,
        "precluster_input_units": precluster_input_units,
        "precluster_embedded_units": precluster_embedded_units,
        "precluster_embed_failed_units": precluster_embed_failed_units,
        "precluster_clusters": precluster_clusters,
        "precluster_singleton_clusters": precluster_singleton_clusters,
        "precluster_oversized_clusters": precluster_oversized_clusters,
        "precluster_claim_empty": precluster_claim_empty,
        "precluster_cluster_member_units": precluster_cluster_member_units,
        "precluster_cluster_member_rows": precluster_cluster_member_rows,
        "precluster_planned_buckets": precluster_planned_buckets,
        "precluster_failed_ratio": precluster_failed_ratio,
        "precluster_failure_allowed": precluster_failure_allowed,
        "precluster_complete": precluster_complete,
        "grounding_checked_attempts": grounding_checked_attempts,
        "grounding_enforced_attempts": grounding_enforced_attempts,
        "grounding_flagged_attempts": grounding_flagged_attempts,
        "grounding_issue_count": grounding_issue_count,
        "grounding_orphan_issues": grounding_orphan_issues,
        "grounding_polarity_issues": grounding_polarity_issues,
        "grounding_title_subject_issues": grounding_title_subject_issues,
        "grounding_short_evidence_issues": grounding_short_evidence_issues,
        "grounding_rejected_groups": grounding_rejected_groups,
        "grounding_complete": grounding_complete,
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


def validate_assignment_projection(run_id: str, ctx) -> dict:
    """校验关系/终态对快照的完整投影，并执行 F=P_unique+D。"""
    verified_snapshot_rows = db.verify_assign_snapshot(run_id)
    snapshot_key_rows = db.load_assign_snapshot_rows(run_id)
    if len(snapshot_key_rows) != verified_snapshot_rows:
        raise RuntimeError(
            "归属快照物理读取行数与指纹校验不一致："
            f"verified={verified_snapshot_rows}, "
            f"loaded={len(snapshot_key_rows)}")
    relation_key_rows = db.load_relation_assignment_rows(run_id)
    terminal_key_rows = db.load_terminal_assignment_rows(run_id)
    rows = db.q("""
      SELECT
        count(*) FILTER (
          WHERE o.opp_type = '老品迭代'
            AND oe.assign_run_id = %s
        )::bigint AS projected_rows,
        count(*) FILTER (
          WHERE o.opp_type = '老品迭代'
            AND (oe.assigned_spu IS NULL
                 OR oe.assignment_source IS NULL
                 OR oe.assign_run_id IS NULL
                 OR oe.assigned_spu IS DISTINCT FROM o.core_tag)
        )::bigint AS bad_old_assignment_rows,
        count(*) FILTER (
          WHERE o.opp_type = '新品创新'
            AND (oe.assigned_spu IS NOT NULL
                 OR oe.assignment_source IS NOT NULL)
        )::bigint AS bad_innovation_assignment_rows,
        count(*) FILTER (
          WHERE o.opp_type = '老品迭代'
            AND NOT EXISTS (
              SELECT 1
                FROM voc_assign_snapshot s
               WHERE s.run_id = oe.assign_run_id
                 AND s.message_id = oe.message_id
                 AND s.seq = oe.seq
                 AND s.assigned_spu = oe.assigned_spu
                 AND s.source = oe.assignment_source
            )
        )::bigint AS missing_snapshot_rows,
        count(*) FILTER (
          WHERE o.opp_type = '老品迭代'
            AND EXISTS (
              SELECT 1
                FROM voc_opp_evidence sibling
               WHERE sibling.opp_id = oe.opp_id
                 AND sibling.assigned_spu IS DISTINCT FROM o.core_tag
            )
        )::bigint AS cross_spu_rows
        FROM voc_opp_evidence oe
        JOIN voc_opportunity o ON o.opp_id = oe.opp_id
       WHERE o.opp_id LIKE 'OPP2-%%'
    """, [run_id])
    raw = rows[0] if rows else {}
    stat = assignment_projection_reconciliation(
        snapshot_key_rows, relation_key_rows, terminal_key_rows,
        max_terminal_loss_ratio=C.TERMINAL_LOSS_MAX_RATIO)
    stat.update({
        "projected_rows": int(raw.get("projected_rows", 0)),
        "bad_old_assignment_rows": int(raw.get("bad_old_assignment_rows", 0)),
        "bad_innovation_assignment_rows": int(
            raw.get("bad_innovation_assignment_rows", 0)),
        "missing_snapshot_rows": int(raw.get("missing_snapshot_rows", 0)),
        "cross_spu_rows": int(raw.get("cross_spu_rows", 0)),
    })
    ctx.metric_update(("finalize",), assignment_projection=stat)
    bad = (
        stat["bad_old_assignment_rows"]
        + stat["bad_innovation_assignment_rows"]
        + stat["missing_snapshot_rows"]
        + stat["cross_spu_rows"]
    )
    if bad or not stat["complete"]:
        detail = _assignment_projection_error(stat)
        if bad:
            detail += (
                "; assignment_metadata_errors="
                f"{{'bad_old': {stat['bad_old_assignment_rows']}, "
                f"'bad_innovation': {stat['bad_innovation_assignment_rows']}, "
                f"'missing_snapshot_source': {stat['missing_snapshot_rows']}, "
                f"'cross_spu': {stat['cross_spu_rows']}}}"
            )
        raise RuntimeError(detail)
    return stat


def refresh_opportunity_neighbors(ctx) -> dict:
    """收尾刷新最近邻缓存，并确保两端机会点均不悬空。"""
    db.execute("SELECT voc_refresh_opp_nn()")
    rows = db.q("""
      SELECT count(*)::bigint AS nn_rows,
             count(*) FILTER (
               WHERE source.opp_id IS NULL OR neighbor.opp_id IS NULL
             )::bigint AS nn_orphan_rows
        FROM voc_opp_nn nn
        LEFT JOIN voc_opportunity source ON source.opp_id = nn.opp_id
        LEFT JOIN voc_opportunity neighbor ON neighbor.opp_id = nn.neighbor_id
    """)
    stat = rows[0] if rows else {"nn_rows": 0, "nn_orphan_rows": 0}
    normalized = {
        "nn_rows": int(stat.get("nn_rows", 0)),
        "nn_orphan_rows": int(stat.get("nn_orphan_rows", 0)),
    }
    ctx.metric_update(("finalize",), **normalized)
    if normalized["nn_orphan_rows"]:
        raise RuntimeError(
            f"机会点最近邻缓存仍有 {normalized['nn_orphan_rows']} 条悬空")
    return normalized


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
    assigned_spus = {
        items[i].get("assigned_spu") for i in members
        if items[i].get("assigned_spu") is not None
    }
    if opp_type == "老品迭代" and assigned_spus != {ctx_info.get("tag")}:
        raise ValueError(
            f"老品分组归属必须唯一等于桶 SPU：bucket={ctx_info.get('tag')!r}, "
            f"assigned={sorted(assigned_spus)!r}")
    if opp_type == "新品创新" and assigned_spus:
        raise ValueError(f"新品分组不得携带 assigned_spu：{sorted(assigned_spus)!r}")
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
    core_tag = ctx_info.get("tag") if opp_type == "老品迭代" else None
    problem_mode = _specific_mode(obj, group["mode_name"])
    return {
        "opp_id": make_opp_id(opp_type, core_tag, problem_mode),
        "opp_type": classification.opp_type,
        "classification_state": classification.classification_state,
        "classify_rule": classification.classify_rule,
        # src_line 仅为兼容旧消费者的单值代表；新的跨来源组
        # 没有唯一「发现方」，故取排序第一个以保证重跑稳定。
        "src_line": source_lines[0],
        "source_lines": source_lines,
        "evi_by_source": dict(sorted(source_counts.items())),
        "prod_line": ctx_info.get("prod_line"),
        "category": ctx_info.get("storage_category"),
        "category_set": cats or None,
        # 兜底占位符不得落库：problem_mode 是向量化字段，写成「未命名模式」会让
        # 该条在 L2 召回里和什么都像，重演吸附器问题。
        "core_tag": core_tag,
        "scope_source": "v3-未计算",
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
# 这三个函数原先只存在于 scripts/run_generate.py 里，旧调度路径把机会点 build
# 出来后只是累加进列表就返回了，【从不落库】。也就是说那条路径跑完什么都没
# 写进去。与 lifecycle.release_to_pm 是同一类问题：逻辑写在脚本里、调度器绕过
# 脚本。提到这里由手工链统一复用。
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

    build 阶段只看得到当前新组；attach 与提案 MERGE 都会改变最终
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
      SELECT m.src_line, m.star,
             p.requires_spu AS source_requires_spu
             , CASE
                 WHEN oe.assign_run_id IS NULL
                      AND voc_has_spu(m.message_id) THEN '__legacy__'
                 ELSE oe.assigned_spu
               END AS assigned_spu
             , oe.assignment_source, oe.assign_run_id
        FROM voc_opp_evidence oe
        JOIN voc_message m ON m.message_id = oe.message_id
        JOIN voc_source_policy p ON p.src_line = m.src_line
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
            # 同一 v3 身份的并发 create 串行化；存量 OPP-* 不在此命名空间。
            c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                      [opp_id])
            existing = c.execute(
                "SELECT opp_id, opp_type, core_tag, problem_mode, "
                "classification_state, merged_into, safety_flag "
                "FROM voc_opportunity "
                "WHERE opp_id=%s FOR UPDATE", [opp_id]).fetchone()
            if existing:
                if existing.get("merged_into"):
                    # 普通 MERGE 源被清空证据后会 recount 成 R0，opp_type
                    # 合法地变为 NULL；不能拿墓碑当前类型重算原 ID。身份内容
                    # 字段仍保留，先用它们排除碰撞。
                    expected_content = (
                        _identity_text(opp.get("core_tag")),
                        _identity_text(opp.get("problem_mode")),
                    )
                    actual_content = (
                        _identity_text(existing.get("core_tag")),
                        _identity_text(existing.get("problem_mode")),
                    )
                    if actual_content != expected_content:
                        raise RuntimeError(f"opp_id 墓碑身份冲突：{opp_id}")
                else:
                    expected = opportunity_identity_key(
                        opp["opp_type"], opp.get("core_tag"),
                        opp.get("problem_mode"))
                    actual = opportunity_identity_key(
                        existing["opp_type"], existing.get("core_tag"),
                        existing.get("problem_mode"))
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
                        "SELECT opp_id, opp_type, core_tag, problem_mode, "
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

        relation_rows = []
        for i in members:
            item = items[i]
            assigned_spu = item.get("assigned_spu")
            assignment_source = item.get("assignment_source")
            assign_run_id = item.get("assign_run_id") or ctx.run_id
            if opp["opp_type"] == "老品迭代":
                if assigned_spu != opp.get("core_tag"):
                    raise ValueError(
                        f"老品关系归属 {assigned_spu!r} 不等于 core_tag "
                        f"{opp.get('core_tag')!r}")
                if assignment_source not in {"fact", "root"}:
                    raise ValueError("老品关系缺少 fact/root assignment_source")
                if assign_run_id != ctx.run_id:
                    raise ValueError("老品关系 assign_run_id 与当前运行不一致")
            elif assigned_spu is not None or assignment_source is not None:
                raise ValueError("新品关系不得携带 SPU 归属")

            relation_rows.append({
                "opp_id": opp_id,
                "message_id": item["message_id"],
                "seq": item.get("seq", 0),
                "attach_week": week,
                "match_by": "rule" if action == "create" else "llm",
                "confidence": round(dec.get("confidence", 1.0), 3),
                "assigned_spu": assigned_spu,
                "assignment_source": assignment_source,
                "assign_run_id": assign_run_id,
            })
        db.upsert_in_transaction(
            c, "voc_opp_evidence", relation_rows,
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
    """额外证据挂载与完整集合回算共用一个事务。"""
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
