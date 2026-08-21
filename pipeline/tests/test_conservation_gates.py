#!/usr/bin/env python3
"""v3 守恒门的离线注入测试：喂真实键集合，不手工伪造 metrics。"""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import config as C, pipeline as P  # noqa: E402


RUN_ID = "offline-conservation"


def _pool_row(message_id: str, spus: tuple[str, ...], seq: int = 0) -> dict:
    return {
        "message_id": message_id,
        "seq": seq,
        "src_line": "电商",
        "source_requires_spu": True,
        "assign_run_id": RUN_ID,
        "spu_assignments": [
            {"assigned_spu": spu, "assignment_source": "fact"}
            for spu in spus
        ],
        "tag": "结构问题",
        "snippet": "支架锁紧后仍然松动",
        "evidence_text": "支架锁紧后仍然松动",
        "full_text": "支架锁紧后仍然松动",
        "content": "支架锁紧后仍然松动",
        "star": 2,
        "category": "支架类",
        "prod_line": "支撑",
        "tax_path": "使用反馈/结构/松动",
        "tax_domain": "使用反馈",
        "tax_sub": "结构",
        "tax_leaf": "松动",
        "is_product": True,
        "sentiment": "负面",
        "low_conf": False,
    }


def _key(message_id: str, spu: str, seq: int = 0) -> dict:
    return {"message_id": message_id, "seq": seq, "assigned_spu": spu}


def _install_route_injection(
    monkeypatch,
    *,
    pool_rows: list[dict],
    snapshot_rows: list[dict],
) -> None:
    monkeypatch.setattr(C, "PRECLUSTER_ENABLED", False)
    monkeypatch.setattr(P.db, "social_structural_gate_counts", lambda *_a, **_k: {})
    monkeypatch.setattr(P.db, "generation_pool", lambda *_a, **_k: pool_rows)
    monkeypatch.setattr(
        P.db, "verify_assign_snapshot", lambda _run_id: len(snapshot_rows))
    monkeypatch.setattr(
        P.db, "load_assign_snapshot_rows", lambda _run_id: snapshot_rows)


def test_route_missing_one_of_n_snapshot_keys_stops_the_round(monkeypatch) -> None:
    _install_route_injection(
        monkeypatch,
        pool_rows=[_pool_row("m1", ("SPU-A",))],
        snapshot_rows=[_key("m1", "SPU-A"), _key("m2", "SPU-B")],
    )

    with pytest.raises(RuntimeError) as raised:
        P.generate_opportunities(
            "2026-W34", C.RunCtx(RUN_ID, "2026-W34"))

    message = str(raised.value)
    assert "F=2, R=1" in message
    assert "snapshot EXCEPT routed=1" in message
    assert "m2" in message and "SPU-B" in message


def test_route_extra_key_not_in_snapshot_stops_the_round(monkeypatch) -> None:
    _install_route_injection(
        monkeypatch,
        pool_rows=[
            _pool_row("m1", ("SPU-A",)),
            _pool_row("m2", ("SPU-B",)),
        ],
        snapshot_rows=[_key("m1", "SPU-A")],
    )

    with pytest.raises(RuntimeError) as raised:
        P.generate_opportunities(
            "2026-W34", C.RunCtx(RUN_ID, "2026-W34"))

    message = str(raised.value)
    assert "F=1, R=2" in message
    assert "routed EXCEPT snapshot=1" in message
    assert "m2" in message and "SPU-B" in message


def test_equal_counts_with_different_key_sets_still_stop_the_round(
    monkeypatch,
) -> None:
    _install_route_injection(
        monkeypatch,
        pool_rows=[
            _pool_row("m1", ("SPU-A",)),
            _pool_row("route-only", ("SPU-C",)),
        ],
        snapshot_rows=[
            _key("m1", "SPU-A"),
            _key("snapshot-only", "SPU-B"),
        ],
    )

    with pytest.raises(RuntimeError) as raised:
        P.generate_opportunities(
            "2026-W34", C.RunCtx(RUN_ID, "2026-W34"))

    message = str(raised.value)
    assert "F=2, R=2" in message
    assert "snapshot EXCEPT routed=1" in message
    assert "routed EXCEPT snapshot=1" in message
    assert "snapshot-only" in message and "route-only" in message


def _install_projection_injection(
    monkeypatch,
    *,
    snapshot_rows: list[dict],
    relation_rows: list[dict],
    terminal_rows: list[dict],
) -> None:
    monkeypatch.setattr(C, "TERMINAL_LOSS_MAX_RATIO", 0.05)
    monkeypatch.setattr(
        P.db, "verify_assign_snapshot", lambda _run_id: len(snapshot_rows))
    monkeypatch.setattr(
        P.db, "load_assign_snapshot_rows", lambda _run_id: snapshot_rows)
    monkeypatch.setattr(
        P.db, "load_relation_assignment_rows", lambda _run_id: relation_rows)
    monkeypatch.setattr(
        P.db, "load_terminal_assignment_rows", lambda _run_id: terminal_rows)
    monkeypatch.setattr(
        P.db, "q", lambda *_a, **_k: [{
            "projected_rows": len(relation_rows),
            "bad_old_assignment_rows": 0,
            "bad_innovation_assignment_rows": 0,
            "missing_snapshot_rows": 0,
            "cross_spu_rows": 0,
        }],
    )


def test_projection_missing_key_without_terminal_record_fails(monkeypatch) -> None:
    snapshot = [_key("m1", "SPU-A"), _key("m2", "SPU-B")]
    _install_projection_injection(
        monkeypatch,
        snapshot_rows=snapshot,
        relation_rows=snapshot[:1],
        terminal_rows=[],
    )

    with pytest.raises(RuntimeError) as raised:
        P.validate_assignment_projection(
            RUN_ID, C.RunCtx(RUN_ID, "2026-W34"))

    message = str(raised.value)
    assert "F=2, P_unique=1, D=0" in message
    assert "既不在关系也不在终态=1" in message
    assert "m2" in message and "SPU-B" in message


def test_projection_gap_is_legal_when_terminal_closes_it_within_limit(
    monkeypatch,
) -> None:
    snapshot = [_key(f"m{index:02d}", "SPU-A") for index in range(20)]
    _install_projection_injection(
        monkeypatch,
        snapshot_rows=snapshot,
        relation_rows=snapshot[:-1],
        terminal_rows=snapshot[-1:],
    )

    stat = P.validate_assignment_projection(
        RUN_ID, C.RunCtx(RUN_ID, "2026-W34"))

    assert stat["complete"] is True
    assert stat["snapshot_except_relation_rows"] == 1
    assert stat["unaccounted_rows"] == 0
    assert stat["snapshot_rows"] == (
        stat["projected_distinct_rows"] + stat["terminal_distinct_rows"])
    assert stat["terminal_loss_ratio"] == pytest.approx(0.05)


def test_terminal_loss_ratio_above_independent_limit_fails(monkeypatch) -> None:
    snapshot = [_key(f"m{index:02d}", "SPU-A") for index in range(10)]
    _install_projection_injection(
        monkeypatch,
        snapshot_rows=snapshot,
        relation_rows=snapshot[:-1],
        terminal_rows=snapshot[-1:],
    )

    with pytest.raises(RuntimeError) as raised:
        P.validate_assignment_projection(
            RUN_ID, C.RunCtx(RUN_ID, "2026-W34"))

    message = str(raised.value)
    assert "F=10, P_unique=9, D=1" in message
    assert "D/F=0.1000 (上限 0.0500)" in message


def test_one_fact_fanned_to_two_spus_writes_two_terminal_rows(
    monkeypatch,
) -> None:
    pool = [_pool_row("fanout", ("SPU-A", "SPU-B"), seq=7)]
    snapshot = [_key("fanout", "SPU-A", 7), _key("fanout", "SPU-B", 7)]
    _install_route_injection(
        monkeypatch, pool_rows=pool, snapshot_rows=snapshot)
    monkeypatch.setattr(P.db, "clear_terminal_evidence", lambda _run_id: 0)
    monkeypatch.setattr(P.db, "q", lambda *_a, **_k: [])

    written: list[dict] = []

    def capture_upsert(_table, rows, _keys, update_cols=None, chunk=1000):
        del update_cols, chunk
        written.extend(dict(row) for row in rows)
        return len(rows)

    monkeypatch.setattr(P.db, "upsert", capture_upsert)

    def fake_split(items, opp_type, info, ctx, vote=None):
        del vote
        ctx.metric_update(
            ("stage1", opp_type, "buckets", str(info["bucket_key"])),
            bucket=str(info["bucket_key"]), status="completed", rounds=1,
            input_rows=len(items), accounted_rows=len(items),
            planned_batches=1, completed_batches=1, failed_batches=0,
            cancelled_batches=0, planned_batch_rows=len(items),
            completed_batch_rows=len(items), failed_batch_rows=0,
            cancelled_batch_rows=0,
        )
        return {
            "groups": [], "dropped": [],
            "unclassified": list(range(len(items))), "rounds": 1,
        }

    monkeypatch.setattr(P.stage1, "split_bucket", fake_split)

    result = P.generate_opportunities(
        "2026-W34", C.RunCtx(RUN_ID, "2026-W34"))

    assert result["reconciliation"]["complete"] is True
    assert len(written) == 2
    assert {
        (row["message_id"], row["seq"], row["assigned_spu"])
        for row in written
    } == {
        ("fanout", 7, "SPU-A"),
        ("fanout", 7, "SPU-B"),
    }
    assert {row["reason"] for row in written} == {"unclassified"}
    assert {row["run_id"] for row in written} == {RUN_ID}


def test_probe_reports_nonempty_bidirectional_diff(capsys) -> None:
    script = (pathlib.Path(__file__).resolve().parents[1]
              / "scripts" / "probe_conservation.py")
    spec = importlib.util.spec_from_file_location("probe_conservation_test", script)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)

    stat = probe.print_conservation(
        [_key("m1", "SPU-A"), _key("snapshot-only", "SPU-B")],
        [_key("m1", "SPU-A"), _key("route-only", "SPU-C")],
    )

    output = capsys.readouterr().out
    assert stat["complete"] is False
    assert "✗ 守恒破坏" in output
    assert "snapshot EXCEPT routed" in output
    assert "routed EXCEPT snapshot" in output
    assert "snapshot-only" in output and "route-only" in output
