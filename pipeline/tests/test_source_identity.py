#!/usr/bin/env python3
"""新来源的 message_id 全局身份契约；全程 mock，不连库。"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import db  # noqa: E402


def test_same_batch_cross_source_message_id_collision_fails_before_query(monkeypatch) -> None:
    query = lambda *args, **kwargs: pytest.fail(  # noqa: E731
        "同批冲突不应读库")
    monkeypatch.setattr(db, "q", query)

    with pytest.raises(ValueError, match="跨来源冲突"):
        db.save_messages([
            {"message_id": "shared-1", "src_line": "电商"},
            {"message_id": "shared-1", "src_line": "问卷调研"},
        ])


def test_existing_other_source_message_id_collision_never_upserts(monkeypatch) -> None:
    monkeypatch.setattr(
        db, "q", lambda *args, **kwargs: [
            {"message_id": "shared-2", "src_line": "社媒"}
        ])
    monkeypatch.setattr(
        db, "upsert", lambda *args, **kwargs: pytest.fail("冲突不应 upsert"))

    with pytest.raises(ValueError, match="已属于其他来源"):
        db.save_messages([
            {"message_id": "shared-2", "src_line": "问卷调研"}
        ])
