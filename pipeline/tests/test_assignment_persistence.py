#!/usr/bin/env python3
"""v3 归属从生成落库到 MERGE 的离线行为契约。"""
from __future__ import annotations

import pathlib
import re
import sys
from contextlib import contextmanager

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import execute, pipeline  # noqa: E402


class _Result:
    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Connection:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params=None) -> _Result:
        self.calls.append((sql, params))
        return _Result()


class _Ctx:
    run_id = "run-v3"


def test_save_opportunity_persists_complete_assignment_projection(monkeypatch) -> None:
    connection = _Connection()
    written: dict[str, list[dict]] = {}

    @contextmanager
    def fake_conn():
        yield connection

    def fake_upsert(_connection, table, rows, _keys, *_args, **_kwargs):
        written.setdefault(table, []).extend(dict(row) for row in rows)
        return len(rows)

    monkeypatch.setattr(pipeline.db, "conn", fake_conn)
    monkeypatch.setattr(pipeline.db, "upsert_in_transaction", fake_upsert)
    monkeypatch.setattr(pipeline.resolve, "resolve_one", lambda _opp, _ctx: {
        "action": "create", "opp_id": None, "proposals": [],
    })
    monkeypatch.setattr(pipeline.llm, "ensure_available", lambda: None)
    monkeypatch.setattr(pipeline, "recount", lambda *_a, **_k: None)

    opp_id = pipeline.save_opportunity(
        {
            "opp_id": "OPP2-OFFLINE", "opp_type": "老品迭代",
            "core_tag": "SPU-A", "problem_mode": "按键回弹失效",
            "mode_vec": [1.0, 0.0], "scope_source": "v3-未计算",
            "_members": [0], "_review": {},
        },
        [{
            "message_id": "m1", "seq": 2,
            "assigned_spu": "SPU-A", "assignment_source": "root",
            "assign_run_id": "run-v3",
        }],
        "2026-W34", _Ctx(),
    )

    assert opp_id == "OPP2-OFFLINE"
    relation = written["voc_opp_evidence"]
    assert relation == [{
        "opp_id": "OPP2-OFFLINE", "message_id": "m1", "seq": 2,
        "attach_week": "2026-W34", "match_by": "rule", "confidence": 1.0,
        "assigned_spu": "SPU-A", "assignment_source": "root",
        "assign_run_id": "run-v3",
    }]


def test_merge_sql_copies_all_assignment_columns_unchanged(monkeypatch) -> None:
    connection = _Connection()
    recounts: list[str] = []
    monkeypatch.setattr(execute.pipeline, "lock_opportunities", lambda *_a: None)
    monkeypatch.setattr(
        execute.pipeline, "recount",
        lambda opp_id, _connection: recounts.append(opp_id),
    )

    result = execute._merge(
        {
            "opp_ids": ["OPP2-TARGET", "OPP2-SOURCE"],
            "payload": {"target": "OPP2-TARGET"},
        },
        connection,
    )

    insert_sql = next(
        re.sub(r"\s+", " ", sql).strip()
        for sql, _params in connection.calls
        if "INSERT INTO voc_opp_evidence" in sql
    )
    assert "assigned_spu, assignment_source, assign_run_id" in insert_sql
    assert "SELECT %s, message_id, seq, attach_week, 'merge', confidence, assigned_spu, assignment_source, assign_run_id" in insert_sql
    assert result and result["target"] == "OPP2-TARGET"
    assert recounts == ["OPP2-TARGET", "OPP2-SOURCE"]
