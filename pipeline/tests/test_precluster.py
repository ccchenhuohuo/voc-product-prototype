#!/usr/bin/env python3
"""归一化诉求预聚类的纯内存测试；embedding 全部用桩。"""
from __future__ import annotations

import gzip
import json
import math
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics import config as C  # noqa: E402
from voc_analytics.config import RunCtx  # noqa: E402
from voc_analytics.stages import precluster  # noqa: E402


def _row(message_id: str, claim: str, seq: int = 0) -> dict:
    return {"message_id": message_id, "seq": seq, "_claim": claim}


def _unit(angle: float) -> list[float]:
    return [math.cos(angle), math.sin(angle)]


def _sequential(monkeypatch) -> None:
    monkeypatch.setattr(
        precluster.llm, "parallel_map",
        lambda fn, items, **_kwargs: [fn(item) for item in items],
    )


def test_same_message_is_one_unit_and_all_rows_reach_its_bucket(monkeypatch) -> None:
    _sequential(monkeypatch)
    seen: list[list[str]] = []

    def embed(claims):
        seen.append(list(claims))
        return [[1.0, 0.0] for _ in claims]

    monkeypatch.setattr(precluster.llm, "embed", embed)
    rows = [_row("m1", "后一条诉求", 2), _row("m1", "最小 seq 诉求", 1)]
    result = precluster.cluster_claims(rows, RunCtx("dedup", "2026-W34"))

    assert seen == [["最小 seq 诉求"]]
    assert result["stats"]["input_units"] == 1
    assert result["stats"]["input_rows"] == 2
    assert result["stats"]["cluster_member_rows"] == 2
    assert sum(map(len, result["clusters"].values())) == 2
    assert len(set(result["assignments"].values())) == 1


def test_same_input_has_byte_stable_cluster_assignment(monkeypatch) -> None:
    _sequential(monkeypatch)
    vectors = {"a": _unit(0.0), "b": _unit(0.1), "c": _unit(1.2)}
    monkeypatch.setattr(
        precluster.llm, "embed", lambda claims: [vectors[claim] for claim in claims])
    rows = [_row("m3", "c"), _row("m1", "a"), _row("m2", "b")]

    first = precluster.cluster_claims(rows, RunCtx("det-1", "2026-W34"))
    second = precluster.cluster_claims(rows, RunCtx("det-2", "2026-W34"))

    assert json.dumps(first["cluster_units"], ensure_ascii=False) == json.dumps(
        second["cluster_units"], ensure_ascii=False)
    assert first["assignments"] == second["assignments"]


def test_cosine_distance_threshold_connects_005_but_not_030(monkeypatch) -> None:
    _sequential(monkeypatch)
    vectors = {
        "origin": [1.0, 0.0],
        "near": [0.95, math.sqrt(1 - 0.95 ** 2)],
        "far": [0.70, math.sqrt(1 - 0.70 ** 2)],
    }
    monkeypatch.setattr(
        precluster.llm, "embed", lambda claims: [vectors[claim] for claim in claims])
    result = precluster.cluster_claims(
        [_row("m1", "origin"), _row("m2", "near"), _row("m3", "far")],
        RunCtx("threshold", "2026-W34"),
    )

    assignments = result["assignments"]
    assert assignments[("m1", 0)] == assignments[("m2", 0)]
    assert assignments[("m1", 0)] != assignments[("m3", 0)]


def test_oversized_component_is_refined_and_metric_is_recorded(
    monkeypatch,
) -> None:
    _sequential(monkeypatch)
    monkeypatch.setattr(C, "PRECLUSTER_MAX", 2)
    vectors = {str(index): _unit(angle) for index, angle in enumerate(
        [0.0, 0.05, 0.45, 0.50])}
    monkeypatch.setattr(
        precluster.llm, "embed", lambda claims: [vectors[claim] for claim in claims])
    ctx = RunCtx("oversized", "2026-W34")

    result = precluster.cluster_claims(
        [_row(f"m{index}", str(index)) for index in range(4)], ctx)

    sizes = sorted(map(len, result["cluster_units"].values()))
    assert sizes == [2, 2]
    assert result["stats"]["oversized_clusters"] == 1
    assert ctx.metrics["precluster"]["oversized_clusters"] == 1


def test_one_embedding_batch_failure_becomes_singletons(monkeypatch) -> None:
    _sequential(monkeypatch)
    monkeypatch.setattr(C, "EMBED_BATCH", 2)
    calls = 0

    def embed(claims):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise precluster.llm.LLMError("offline batch failure")
        return [[1.0, 0.0] for _ in claims]

    monkeypatch.setattr(precluster.llm, "embed", embed)
    result = precluster.cluster_claims(
        [_row(f"m{index}", str(index)) for index in range(3)],
        RunCtx("batch-failure", "2026-W34"),
    )

    assert result["stats"]["embed_failed_units"] == 2
    assert result["stats"]["embedded_units"] == 1
    assert result["stats"]["clusters"] == 3
    assert result["stats"]["singleton_clusters"] == 3


def test_fatal_embedding_error_is_not_swallowed(monkeypatch) -> None:
    _sequential(monkeypatch)
    monkeypatch.setattr(
        precluster.llm, "embed",
        lambda _claims: (_ for _ in ()).throw(
            precluster.llm.FatalLLMError("fatal offline")),
    )

    with pytest.raises(precluster.llm.FatalLLMError, match="fatal offline"):
        precluster.cluster_claims(
            [_row("m1", "需要某个配件")], RunCtx("fatal", "2026-W34"))


def test_golden_200_claims_match_calibrated_graph(monkeypatch) -> None:
    _sequential(monkeypatch)
    fixture = pathlib.Path(__file__).parent / "fixtures" / "precluster_golden.json.gz"
    with gzip.open(fixture, "rt", encoding="utf-8") as stream:
        golden = json.load(stream)
    rows = [_row(item["cid"], item["claim"]) for item in golden]
    vectors = [item["vec"] for item in golden]
    offset = 0

    def embed(claims):
        nonlocal offset
        batch = vectors[offset:offset + len(claims)]
        offset += len(claims)
        return batch

    monkeypatch.setattr(precluster.llm, "embed", embed)
    result = precluster.cluster_claims(rows, RunCtx("golden", "2026-W34"))
    sizes = [len(units) for units in result["cluster_units"].values()]

    assert len(sizes) == 189
    assert sum(size > 1 for size in sizes) == 6
    assert max(sizes) == 7
    assert sum(size == 1 for size in sizes) == 183
