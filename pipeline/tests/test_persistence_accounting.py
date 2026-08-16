#!/usr/bin/env python3
"""持久化中途失败时，已提交/失败/未启动不得混记。"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import pipeline  # noqa: E402
from voc_analytics.config import RunCtx  # noqa: E402


def test_partial_commit_is_counted_per_opportunity(monkeypatch) -> None:
    ctx = RunCtx(run_id="offline-persist", week="2026-W34")
    ctx.metric_update(
        ("generation",), planned_persistence=3,
        completed_persistence=0, failed_persistence=0)
    pairs = [
        ({"problem_mode": f"mode-{index}"}, [])
        for index in range(3)
    ]
    monkeypatch.setattr(
        pipeline.llm, "embed", lambda texts: [[float(i)] for i, _ in enumerate(texts)])
    attempts = 0

    def save(opp, items, week, run_ctx):
        nonlocal attempts
        del opp, items, week, run_ctx
        attempts += 1
        if attempts == 2:
            raise RuntimeError("offline DB failure")
        return "OPP2-FIRST"

    monkeypatch.setattr(pipeline, "save_opportunity", save)

    with pytest.raises(RuntimeError, match="offline DB failure"):
        pipeline.persist_opportunities(pairs, ctx.week, ctx, account=True)

    ledger = ctx.metrics["generation"]
    assert ledger["completed_persistence"] == 1
    assert ledger["failed_persistence"] == 1
    assert (
        ledger["planned_persistence"]
        - ledger["completed_persistence"]
        - ledger["failed_persistence"]
    ) == 1


def test_shared_cancel_is_checked_before_embedding_or_database(monkeypatch) -> None:
    ctx = RunCtx(run_id="offline-cancel", week="2026-W34")
    embed = pytest.fail
    save = pytest.fail
    monkeypatch.setattr(
        pipeline.llm, "ensure_available",
        lambda: (_ for _ in ()).throw(pipeline.llm.FatalLLMError("peer fatal")))
    monkeypatch.setattr(pipeline.llm, "embed", embed)
    monkeypatch.setattr(pipeline, "save_opportunity", save)

    with pytest.raises(pipeline.llm.FatalLLMError, match="peer fatal"):
        pipeline.persist_opportunities(
            [({"problem_mode": "mode"}, [])], ctx.week, ctx, account=True)

    assert ctx.metrics["generation"]["failed_persistence"] == 1
