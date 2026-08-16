#!/usr/bin/env python3
"""L1 候选键按生命周期分支的离线测试。"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import resolve  # noqa: E402


def test_old_product_l1_key_uses_type_and_tag_without_source(monkeypatch) -> None:
    calls: list[tuple[str, list]] = []

    def fake_q(sql: str, params: list) -> list[dict]:
        calls.append((sql, params))
        return [{"opp_id": "OPP2-OFFLINE"}]

    monkeypatch.setattr(resolve.db, "q", fake_q)

    assert resolve.l1_candidates("按键卡滞", "老品迭代", "产品体验") == [
        {"opp_id": "OPP2-OFFLINE"}
    ]
    sql, params = calls.pop()

    assert "opp_type IS NOT DISTINCT FROM %s" in sql
    assert "core_tag IS NOT DISTINCT FROM %s" in sql
    assert "channel IS NOT DISTINCT FROM %s" not in sql
    assert "src_line" not in sql
    assert params == ["按键卡滞", "老品迭代"]


def test_innovation_l1_key_uses_lifecycle_channel_not_source(monkeypatch) -> None:
    calls: list[tuple[str, list]] = []
    monkeypatch.setattr(
        resolve.db,
        "q",
        lambda sql, params: calls.append((sql, params)) or [],
    )

    resolve.l1_candidates("自定义功能", "新品创新", "需求缺口")
    sql, params = calls.pop()

    assert "channel IS NOT DISTINCT FROM %s" in sql
    assert "opp_type IS NOT DISTINCT FROM %s" in sql
    assert "src_line" not in sql
    assert params == ["需求缺口", "自定义功能", "新品创新"]


def test_l1_rejects_unknown_lifecycle_before_query(monkeypatch) -> None:
    query = pytest.fail
    monkeypatch.setattr(resolve.db, "q", query)

    with pytest.raises(ValueError, match="未知机会类型"):
        resolve.l1_candidates("标签", "不存在的生命周期", None)
