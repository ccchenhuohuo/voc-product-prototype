#!/usr/bin/env python3
"""统一证据池 SQL 结构契约；只捕获字符串，绝不执行 SQL。"""
from __future__ import annotations

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import config as C  # noqa: E402
from voc_analytics import db  # noqa: E402


def test_generation_pool_is_content_first_and_policy_gated(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def capture_only(sql: str, params: list) -> list[dict]:
        captured.update(sql=sql, params=params)
        return []

    monkeypatch.setattr(db, "q", capture_only)

    assert db.generation_pool("2026-08-10", "2026-08-17") == []
    sql = re.sub(r"\s+", " ", str(captured["sql"])).strip()
    params = list(captured["params"])

    # 用户使用体验作为显式内容分支入池，而非依赖来源名。
    assert list(C.DEWATER_EXPERIENCE) in params
    assert "m.content_type" in sql
    assert "r.experience_tags" in sql
    assert "e.tag = ANY(r.experience_tags)" not in sql
    assert "e.sentiment = '负面'" in sql
    assert "NULLIF(btrim(e.snippet), '') IS NOT NULL" in sql
    assert "COALESCE(NULLIF(btrim(e.snippet), ''), NULLIF(btrim(m.content), ''))" in sql

    # R3 由来源能力属性表达，新增来源无需改写电商/社媒二分。
    assert "JOIN voc_source_policy p USING (src_line)" in sql
    assert "NOT p.requires_spu" in sql
    assert "cardinality(m.spu)" in sql
    assert not re.search(r"m\.src_line\s*=", sql, re.I)
    assert not re.search(r"m\.src_line\s+IN\s*\(", sql, re.I)
    assert "'电商'" not in sql
    assert "'社媒'" not in sql

    assert params[-2:] == ["2026-08-10", "2026-08-17"]
