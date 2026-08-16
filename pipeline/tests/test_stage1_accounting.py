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



def _parse_batch(monkeypatch, modes, unclassified, idx):
    payload = {"modes": modes, "unclassified": unclassified}
    monkeypatch.setattr(stage1.llm, "chat_json", lambda *a, **k: (payload, {}))
    items = [{"snippet": f"s{i}", "tag": "t", "message_id": f"m{i}", "seq": 1}
             for i in idx]
    return stage1.split_batch(items, "老品迭代", idx, {}, 1, 1)


def test_duplicate_assignment_is_tolerated_and_counted(monkeypatch) -> None:
    """一条抱怨同时命中两个失效模式是本设计的预期输入，不是异常。

    Stage1 架构是「分批 × 投票 → 共现 → 最大团」，证据在多次归属中的共现
    正是构图信号，重叠由最大团一步收敛。2c 曾在此加严格一一对应断言，
    2026-08-17 单周探针实测：第一批就因 duplicated=1 整批失败，
    全周只跑了 19 次调用就中止。
    """
    out = _parse_batch(
        monkeypatch,
        [{"mode_name": "松动", "evidence_idx": [1, 2]},
         {"mode_name": "断裂", "evidence_idx": [2]}],
        [],
        [0, 1],
    )
    assert out["duplicated"] == 1
    assert set(out["modes"]) == {"松动", "断裂"}


def test_missing_assignment_falls_into_unclassified(monkeypatch) -> None:
    """遗漏归进 unclassified 并计数，不中止整轮。

    unclassified 本就是「归不了类的证据」这个语义桶，遗漏落进去不丢数据、
    可审计、下游已有处理。中止的代价是整轮跑不完：一轮全量跨几千次调用，
    要求 LLM 零遗漏概率为零。2026-08-17 探针实测 missing=2/10 就中止在
    第 89 次调用。防「部分失败记成全量成功」的是批次级账本，不是这里。
    """
    out = _parse_batch(monkeypatch,
                       [{"mode_name": "松动", "evidence_idx": [1]}], [], [0, 1])
    assert out["missing"] == 1
    assert out["unclassified"] == [1]          # 未被归属的原始下标
    assert out["modes"]["松动"] == [0]
    # 守恒：归属 + 未归类 == 输入
    covered = {m for picks in out["modes"].values() for m in picks} | set(out["unclassified"])
    assert covered == {0, 1}
