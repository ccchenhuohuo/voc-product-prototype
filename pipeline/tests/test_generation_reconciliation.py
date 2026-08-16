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
    ctx.metric_update(("generation",), **values)
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


def test_selected_gate_and_stage1_rows_form_one_conservation_equation() -> None:
    ctx = _ctx_with_generation(
        planned_buckets=2, completed_buckets=2, failed_buckets=0,
        cancelled_buckets=0, planned_groups=0, completed_groups=0,
        failed_groups=0, cancelled_groups=0, planned_persistence=0,
        completed_persistence=0, failed_persistence=0,
        selected_evidence_rows=5, intent_gate_input_rows=4,
        intent_gate_passed_rows=2, intent_gate_rejected_rows=2,
        intent_gate_failed_rows=0,
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
    assert result["selected_evidence_rows"] == (
        result["stage1_input_rows"] + result["intent_gate_rejected_rows"]
    )
