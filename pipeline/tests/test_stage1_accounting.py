#!/usr/bin/env python3
"""Stage1 失败即停及批次/行账本闭环的离线测试。"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics import config as C  # noqa: E402
from voc_analytics.config import RunCtx  # noqa: E402
from voc_analytics.stages import stage1  # noqa: E402


def _sequential_map(fn, items, workers=None):
    del workers
    return [fn(item) for item in items]


def test_split_batch_failure_aborts_bucket_and_closes_batch_row_ledger(
    monkeypatch,
) -> None:
    # 比单批多 1 条：第一批失败后，剩余 1 条必须记为取消。
    items = [
        {"message_id": f"offline-{index}", "seq": 1, "evidence_text": "占位证据"}
        for index in range(C.BATCH_SIZE + 1)
    ]
    ctx = RunCtx(run_id="offline-stage1", week="2026-W34")
    original = RuntimeError("offline batch failure")

    monkeypatch.setattr(stage1.llm, "parallel_map", _sequential_map)

    def fail_without_llm(*args, **kwargs):
        del args, kwargs
        raise original

    monkeypatch.setattr(stage1, "split_batch", fail_without_llm)

    with pytest.raises(RuntimeError) as caught:
        stage1.split_bucket(
            items,
            "老品迭代",
            {"bucket_key": "offline-old-product"},
            ctx,
            vote=False,
        )

    assert caught.value is original
    buckets = ctx.metrics["stage1"]["老品迭代"]["buckets"]
    assert len(buckets) == 1
    ledger = next(iter(buckets.values()))

    assert ledger["status"] == "failed"
    assert ledger["planned_batches"] == 2
    assert ledger["completed_batches"] == 0
    assert ledger["failed_batches"] == 1
    assert ledger["cancelled_batches"] == 1
    assert ledger["planned_batches"] == (
        ledger["completed_batches"]
        + ledger["failed_batches"]
        + ledger["cancelled_batches"]
    )
    assert ledger["planned_batch_rows"] == len(items)
    assert ledger["completed_batch_rows"] == 0
    assert ledger["failed_batch_rows"] == C.BATCH_SIZE
    assert ledger["cancelled_batch_rows"] == 1
    assert ledger["planned_batch_rows"] == (
        ledger["completed_batch_rows"]
        + ledger["failed_batch_rows"]
        + ledger["cancelled_batch_rows"]
    )
    assert ctx.llm_failed_modes == 1

