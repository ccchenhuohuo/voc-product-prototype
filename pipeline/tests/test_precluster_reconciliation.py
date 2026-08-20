#!/usr/bin/env python3
"""预聚类层的守恒与边界对账。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics.config import RunCtx  # noqa: E402
from voc_analytics.pipeline import generation_reconciliation  # noqa: E402


def _closed_ctx(*, stage1_rows: int = 0, **overrides) -> RunCtx:
    ctx = RunCtx("precluster-reconcile", "2026-W34")
    values = {
        "planned_buckets": 0, "completed_buckets": 0,
        "failed_buckets": 0, "cancelled_buckets": 0,
        "planned_groups": 0, "completed_groups": 0,
        "failed_groups": 0, "cancelled_groups": 0,
        "planned_persistence": 0, "completed_persistence": 0,
        "failed_persistence": 0, "cancelled_persistence": 0,
        "selected_evidence_rows": 0,
        "precluster_enabled": True,
        "precluster_target_rows": 0,
        "precluster_input_rows": 0, "precluster_input_units": 0,
        "precluster_embedded_units": 0,
        "precluster_embed_failed_units": 0,
        "precluster_clusters": 0, "precluster_singleton_clusters": 0,
        "precluster_oversized_clusters": 0, "precluster_claim_empty": 0,
        "precluster_cluster_member_units": 0,
        "precluster_cluster_member_rows": 0,
        "precluster_planned_buckets": 0,
    }
    values.update(overrides)
    ctx.metric_update(("generation",), **values)
    if stage1_rows:
        ctx.metric_update(
            ("stage1", "mixed", "buckets", "offline"),
            status="completed", input_rows=stage1_rows,
            accounted_rows=stage1_rows, planned_batches=1,
            completed_batches=1, failed_batches=0, cancelled_batches=0,
            planned_batch_rows=stage1_rows,
            completed_batch_rows=stage1_rows,
            failed_batch_rows=0, cancelled_batch_rows=0,
        )
    return ctx


def test_all_four_precluster_identities_close_independently() -> None:
    ctx = _closed_ctx(
        stage1_rows=5,
        planned_buckets=3, completed_buckets=3,
        selected_evidence_rows=5,
        precluster_target_rows=3,
        precluster_claim_empty=0, precluster_input_rows=3,
        precluster_input_units=2, precluster_embedded_units=1,
        precluster_embed_failed_units=1,
        precluster_clusters=2, precluster_singleton_clusters=2,
        precluster_cluster_member_units=2,
        precluster_cluster_member_rows=3,
        precluster_planned_buckets=2,
    )
    result = generation_reconciliation(ctx)

    assert result["precluster_target_rows"] == result["precluster_input_rows"]
    assert result["precluster_input_units"] == (
        result["precluster_embedded_units"]
        + result["precluster_embed_failed_units"])
    assert result["precluster_cluster_member_units"] == (
        result["precluster_embedded_units"]
        + result["precluster_embed_failed_units"])
    assert result["precluster_planned_buckets"] == result["precluster_clusters"]
    assert result["complete"] is True


def test_zero_input_is_a_closed_precluster_ledger() -> None:
    result = generation_reconciliation(_closed_ctx())
    assert result["precluster_complete"] is True
    assert result["complete"] is True


def test_claim_empty_leak_from_value_gate_is_not_a_second_terminal_state() -> None:
    result = generation_reconciliation(_closed_ctx(
        selected_evidence_rows=4,
        precluster_target_rows=4,
        precluster_claim_empty=4,
    ))
    assert result["precluster_claim_empty"] == 4
    assert result["precluster_clusters"] == 0
    assert result["precluster_complete"] is False
    assert result["complete"] is False


def test_all_embedding_failures_still_form_singletons_and_close_when_allowed() -> None:
    result = generation_reconciliation(_closed_ctx(
        stage1_rows=2,
        planned_buckets=2, completed_buckets=2,
        selected_evidence_rows=2,
        precluster_target_rows=2,
        precluster_input_rows=2, precluster_input_units=2,
        precluster_embed_failed_units=2,
        precluster_clusters=2, precluster_singleton_clusters=2,
        precluster_cluster_member_units=2,
        precluster_cluster_member_rows=2,
        precluster_planned_buckets=2,
    ))
    assert result["precluster_failure_allowed"] is True
    assert result["complete"] is True


def test_systemic_embedding_failure_uses_existing_ratio_and_absolute_floor() -> None:
    result = generation_reconciliation(_closed_ctx(
        stage1_rows=3,
        planned_buckets=3, completed_buckets=3,
        selected_evidence_rows=3,
        precluster_target_rows=3,
        precluster_input_rows=3, precluster_input_units=3,
        precluster_embed_failed_units=3,
        precluster_clusters=3, precluster_singleton_clusters=3,
        precluster_cluster_member_units=3,
        precluster_cluster_member_rows=3,
        precluster_planned_buckets=3,
    ))
    assert result["precluster_failure_allowed"] is False
    assert result["precluster_complete"] is False
    assert result["complete"] is False
