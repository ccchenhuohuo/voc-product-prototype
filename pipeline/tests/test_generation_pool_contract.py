#!/usr/bin/env python3
"""G1--G3 纯函数与生成池 SQL 结构契约；绝不执行 SQL。"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import config as C, db  # noqa: E402


def _social(**values: object) -> dict:
    return {
        "brands": [],
        "content_type": ["用户咨询"],
        "author_name": "普通用户",
        "message_group_id": "g1",
        **values,
    }


def test_g1_rejects_any_non_own_brand_without_competitor_dictionary() -> None:
    assert db.social_structural_terminal(
        _social(brands=["VIJIM", "任意未知品牌"])
    ) == db.SOCIAL_G1_TERMINAL
    assert db.social_structural_terminal(
        _social(brands=["VIJIM", "宙比", "小隼"])
    ) is None


def test_g1b_rejects_comment_when_group_root_contains_competitor() -> None:
    comment = _social(brands=[])
    root = _social(message_type="帖子", brands=["竞品X"])
    unrelated = _social(message_group_id="g2", message_type="帖子", brands=["竞品Y"])

    assert db.social_structural_terminal(comment, [root]) == db.SOCIAL_G1_TERMINAL
    assert db.social_structural_terminal(comment, [unrelated]) is None


@pytest.mark.parametrize(
    "content_type",
    [
        ["产品种草广告", "用户咨询"],
        ["产品评测"],
        ["竞品拉踩"],
        ["二手转让"],
        ["其他"],
        [],
    ],
)
def test_g2_drop_tag_veto_and_keep_tag_requirement(content_type: list[str]) -> None:
    assert db.social_structural_terminal(
        _social(content_type=content_type)
    ) == db.SOCIAL_G2_TERMINAL


@pytest.mark.parametrize("keep", C.SOCIAL_KEEP_TAGS)
def test_g2_keep_tags_pass(keep: str) -> None:
    assert db.social_structural_terminal(_social(content_type=[keep])) is None


@pytest.mark.parametrize("alias", C.BRAND_OWN_ALIASES)
def test_g3_official_author_pattern_is_derived_from_all_own_aliases(alias: str) -> None:
    assert db.social_structural_terminal(
        _social(author_name=f"{alias}官方账号")
    ) == db.SOCIAL_G3_TERMINAL


def test_generation_pool_has_unchanged_ecommerce_and_new_social_entry(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def capture_only(sql: str, params) -> list[dict]:
        captured.update(sql=sql, params=params)
        return []

    monkeypatch.setattr(db, "q", capture_only)
    assert db.generation_pool("2026-08-10", "2026-08-17") == []

    sql = re.sub(r"\s+", " ", str(captured["sql"])).strip()
    params = dict(captured["params"])

    assert "m.src_line = '电商'" in sql
    assert "e.is_product" in sql
    assert "e.sentiment = '负面'" in sql
    assert "NULLIF(btrim(e.snippet), '') IS NOT NULL" in sql
    assert "NOT p.requires_spu OR COALESCE(cardinality(m.spu), 0) > 0" in sql

    assert "m.src_line = '社媒'" in sql
    assert "b <> ALL(%(own_brands)s)" in sql
    assert "p.message_type IN ('帖子','视频')" in sql
    assert "b2 <> ALL(%(own_brands)s)" in sql
    assert "%(drop_tags)s" in sql and "%(keep_tags)s" in sql
    assert "m.author_name !~* %(official_pattern)s" in sql
    assert "m.message_type IN ('评论','回复')" in sql
    assert "parent.message_title" in sql
    assert "m.spu_inherited" in sql
    assert "content_branch" not in sql
    assert "comparison" not in sql
    assert params["own_brands"] == list(C.OWN_BRANDS)
    assert params["drop_tags"] == list(C.SOCIAL_DROP_TAGS)
    assert params["keep_tags"] == list(C.SOCIAL_KEEP_TAGS)
    assert params["official_pattern"] == C.OFFICIAL_AUTHOR_PATTERN
    assert params["week_start"] == "2026-08-10"
    assert params["week_end"] == "2026-08-17"


def test_structural_count_query_reuses_the_same_conditions(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        db, "q",
        lambda sql, params: captured.update(sql=sql, params=params) or [],
    )

    result = db.social_structural_gate_counts()
    sql = str(captured["sql"])

    assert result["social_candidate_rows"] == 0
    for fragment in (
        "b <> ALL(%(own_brands)s)", "b2 <> ALL(%(own_brands)s)",
        "%(drop_tags)s", "%(keep_tags)s", "%(official_pattern)s",
    ):
        assert fragment in sql
    assert "WHERE NOT g1_ok" in sql
    assert "g1_ok AND NOT g2_ok" in sql
    assert "g1_ok AND g2_ok AND NOT g3_ok" in sql
