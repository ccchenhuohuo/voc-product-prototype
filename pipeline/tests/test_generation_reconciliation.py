#!/usr/bin/env python3
"""生成账本的纯内存对账测试。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics.config import RunCtx  # noqa: E402
from voc_analytics.pipeline import generation_reconciliation  # noqa: E402


def _ctx_with_generation(**values: int) -> RunCtx:
    ctx = RunCtx(run_id="offline-reconcile", week="2026-W34")
    assignment = {
        "assignment_snapshot_rows": 0,
        "assignment_unique_facts": 0,
        "assignment_expected_rows": 0,
        "assignment_routed_rows": 0,
        "assignment_conservation": True,
    }
    assignment.update(values)
    ctx.metric_update(("generation",), **assignment)
    return ctx


def test_reconciliation_marks_closed_success_ledger_complete() -> None:
    ctx = _ctx_with_generation(
        planned_buckets=2,
        completed_buckets=2,
        failed_buckets=0,
        cancelled_buckets=0,
        planned_groups=3,
        completed_groups=3,
        failed_groups=0,
        cancelled_groups=0,
        planned_persistence=3,
        completed_persistence=3,
        failed_persistence=0,
    )

    result = generation_reconciliation(ctx)

    assert result["complete"] is True
    assert result["planned_buckets"] == result["completed_buckets"]
    assert result["planned_groups"] == result["completed_groups"]
    assert result["planned_persistence"] == result["completed_persistence"]
    assert ctx.metrics["reconciliation"] == result


def test_reconciliation_closes_failed_ledger_but_never_calls_it_complete() -> None:
    ctx = _ctx_with_generation(
        planned_buckets=3,
        completed_buckets=1,
        failed_buckets=1,
        cancelled_buckets=1,
        planned_groups=4,
        completed_groups=2,
        failed_groups=1,
        cancelled_groups=1,
        planned_persistence=2,
        completed_persistence=1,
        failed_persistence=1,
    )

    result = generation_reconciliation(ctx)

    assert result["complete"] is False
    assert result["planned_buckets"] == (
        result["completed_buckets"]
        + result["failed_buckets"]
        + result["cancelled_buckets"]
    )
    assert result["planned_groups"] == (
        result["completed_groups"]
        + result["failed_groups"]
        + result["cancelled_groups"]
    )
    assert result["planned_persistence"] == (
        result["completed_persistence"] + result["failed_persistence"]
    )


def test_reconciliation_detects_silent_count_gap() -> None:
    ctx = _ctx_with_generation(
        planned_buckets=1,
        completed_buckets=0,
        failed_buckets=0,
        cancelled_buckets=0,
        planned_groups=0,
        completed_groups=0,
        failed_groups=0,
        cancelled_groups=0,
        planned_persistence=0,
        completed_persistence=0,
        failed_persistence=0,
    )

    assert generation_reconciliation(ctx)["complete"] is False


def test_reconciliation_rejects_a_deliberately_truncated_scope() -> None:
    ctx = _ctx_with_generation(
        planned_buckets=1, completed_buckets=1, failed_buckets=0,
        cancelled_buckets=0, planned_groups=0, completed_groups=0,
        failed_groups=0, cancelled_groups=0, planned_persistence=0,
        completed_persistence=0, failed_persistence=0,
        scope_truncated_buckets=2, scope_truncated_rows=17,
    )

    result = generation_reconciliation(ctx)

    assert result["complete"] is False
    assert result["scope_truncated_buckets"] == 2
    assert result["scope_truncated_rows"] == 17


def test_selected_and_stage1_rows_form_one_conservation_equation() -> None:
    ctx = _ctx_with_generation(
        planned_buckets=2, completed_buckets=2, failed_buckets=0,
        cancelled_buckets=0, planned_groups=0, completed_groups=0,
        failed_groups=0, cancelled_groups=0, planned_persistence=0,
        completed_persistence=0, failed_persistence=0,
        selected_evidence_rows=3,
    )
    ctx.metric_update(
        ("stage1", "新品创新", "buckets", "offline"),
        status="completed", input_rows=3, accounted_rows=3,
        planned_batches=1, completed_batches=1, failed_batches=0,
        cancelled_batches=0, planned_batch_rows=3,
        completed_batch_rows=3, failed_batch_rows=0,
        cancelled_batch_rows=0,
    )

    result = generation_reconciliation(ctx)

    assert result["complete"] is True
    assert result["selected_evidence_rows"] == result["stage1_input_rows"]


def test_social_seven_terminal_ledger_closes_exactly() -> None:
    ctx = _ctx_with_generation(
        planned_buckets=1, completed_buckets=1, failed_buckets=0,
        cancelled_buckets=0, planned_groups=0, completed_groups=0,
        failed_groups=0, cancelled_groups=0, planned_persistence=0,
        completed_persistence=0, failed_persistence=0,
        selected_evidence_rows=4,
        social_candidate_rows=10,
        g1_competitor_rows=1, g2_dewater_rows=1, g3_official_rows=1,
        structural_passed_rows=7, value_gate_input_rows=7,
        value_gate_input_messages=7, value_gate_cache_hit_messages=3,
        value_gate_llm_messages=4, value_gate_llm_votes=6,
        value_gate_failed_rows=0, g4_no_value_rows=1,
        generic_claim_rows=1, unassigned_defect_rows=1,
        social_old_pool_rows=2, social_innovation_pool_rows=2,
    )
    ctx.metric_update(
        ("stage1", "mixed", "buckets", "offline"),
        status="completed", input_rows=4, accounted_rows=4,
        planned_batches=1, completed_batches=1, failed_batches=0,
        cancelled_batches=0, planned_batch_rows=4,
        completed_batch_rows=4, failed_batch_rows=0,
        cancelled_batch_rows=0,
    )

    result = generation_reconciliation(ctx)

    assert result["social_candidate_rows"] == (
        result["g1_competitor_rows"] + result["g2_dewater_rows"]
        + result["g3_official_rows"] + result["g4_no_value_rows"]
        + result["generic_claim_rows"] + result["unassigned_defect_rows"]
        + result["social_old_pool_rows"]
        + result["social_innovation_pool_rows"]
    )
    assert result["structural_gate_complete"] is True
    assert result["social_terminal_complete"] is True
    assert result["complete"] is True


def test_social_terminal_gap_is_never_silently_accepted() -> None:
    result = generation_reconciliation(_ctx_with_generation(
        social_candidate_rows=2, structural_passed_rows=2,
        value_gate_input_rows=2, value_gate_input_messages=2,
        value_gate_cache_hit_messages=2,
        g4_no_value_rows=1,
    ))

    assert result["social_terminal_complete"] is False
    assert result["complete"] is False


def test_assignment_conservation_is_a_required_completion_gate() -> None:
    result = generation_reconciliation(_ctx_with_generation(
        planned_buckets=0, completed_buckets=0, failed_buckets=0,
        cancelled_buckets=0, planned_groups=0, completed_groups=0,
        failed_groups=0, cancelled_groups=0, planned_persistence=0,
        completed_persistence=0, failed_persistence=0,
        assignment_expected_rows=2, assignment_routed_rows=1,
        assignment_conservation=False,
    ))

    assert result["assignment_conservation"] is False
    assert result["complete"] is False


def test_persistence_runs_inside_the_bucket_worker() -> None:
    """落库必须在桶自己的线程里，且在 completed_buckets 计数之后、try 之外。

    并行化的安全性来自分区：resolve_one 的 L1 只在同一 core_tag 内召回候选
    （resolve.py `core_tag IS NOT DISTINCT FROM %s`），而桶键就是 core_tag，
    不同桶的卡永远不可能互为合并候选，所以区内串行、区间并行不改变去重语义。

    2026-08-19 实测：generate_existing 4h44m 里 Stage1 全量只占 143 秒，
    其余几乎全在此前串行的落库循环（每卡约 3 次 L3、单次 4.2 秒）。
    """
    import inspect

    from voc_analytics import pipeline as P

    src = inspect.getsource(P.generate_opportunities)
    prepare_src = src[src.index("def prepare(entry)"):src.index("created: list[str] = []")]
    assert "persist_opportunities(" in prepare_src, "落库必须在 prepare 内"

    main_loop = src[src.index("created: list[str] = []"):]
    assert "persist_opportunities(" not in main_loop, (
        "主循环不得再自己落库——那正是被并行化掉的串行瓶颈")
    assert 'created.extend(prepared["ids"])' in main_loop


def test_persistence_is_outside_the_failed_bucket_handler() -> None:
    """落库失败只能记 failed_persistence，不能再记 failed_buckets。

    桶在落库前已计入 completed_buckets；若落库异常再走 except 分支加一次
    failed_buckets，就会打破 planned == completed + failed + cancelled 的
    对账不变量，让一次真实失败表现为「对账失败」这种误导性错误。
    """
    import inspect

    from voc_analytics import pipeline as P

    src = inspect.getsource(P.generate_opportunities)
    handler = src.index('ctx.metric_incr(("generation",), failed_buckets=1)')
    persist = src.index("ids = persist_opportunities(")
    assert persist > handler, "落库必须在 failed_buckets 处理段之后"
