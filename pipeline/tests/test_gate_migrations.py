#!/usr/bin/env python3
"""023--025 迁移的静态契约；只读文件，绝不连库或执行 SQL。"""
from __future__ import annotations

import pathlib


SQL = pathlib.Path(__file__).resolve().parents[1] / "sql"


def test_023_adds_thread_facts_cache_roles_and_idempotent_inheritance() -> None:
    text = (SQL / "023_social_thread_columns.sql").read_text()

    for column in (
        "message_group_id text", "message_type text", "parent_id text",
        "author_name text", "message_title text", "spu_inherited text[]",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in text
    assert "CREATE TABLE IF NOT EXISTS voc_social_gate" in text
    assert "CHECK (cls IN ('诉求缺口','产品缺陷','无价值'))" in text
    assert "message_id  text PRIMARY KEY REFERENCES voc_message(message_id) ON DELETE CASCADE" in text
    assert "voc_backfill_social_spu_inheritance" in text
    assert "array_agg(DISTINCT" in text
    assert "cardinality(g.spus) = 1" in text
    assert "COALESCE(cardinality(target.spu), 0) = 0" in text
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON voc_social_gate TO voc_writer" in text
    assert "GRANT SELECT ON voc_social_gate TO voc_human, voc_reader" in text


def test_024_drops_the_column_and_rebuilds_current_views() -> None:
    text = (SQL / "024_drop_channel.sql").read_text()

    assert "必须与全量重跑同批执行" in text
    assert "需先部署配套应用代码" in text
    assert "ALTER TABLE voc_opportunity DROP COLUMN IF EXISTS channel" in text
    assert text.index("DROP VIEW IF EXISTS voc_board") < text.index(
        "ALTER TABLE voc_opportunity DROP COLUMN")
    assert "o.opp_type, o.src_line, o.prod_line" in text
    assert "o.opp_type, o.src_line, o.channel" not in text
    for view in ("voc_board", "voc_inbox", "voc_safety_watch"):
        assert f"CREATE VIEW {view}" in text


def test_025_expands_spu_scope_without_redefining_issue_layer() -> None:
    text = (SQL / "025_spu_scope.sql").read_text()

    assert "COALESCE(m.spu_inherited, ARRAY[]::text[])" in text
    assert "m.src_line = '社媒'" in text
    assert "m.src_line = '电商'" in text
    assert "(e.spu IS NOT NULL) AS has_ec" in text
    assert "CREATE UNIQUE INDEX ux_voc_spu_spu" in text
    assert "DROP MATERIALIZED VIEW IF EXISTS voc_spu_issue" not in text
    assert "CREATE MATERIALIZED VIEW voc_spu_issue" not in text


def test_029_inheritance_is_root_only() -> None:
    """继承的事实 SPU 采集范围必须收敛到原帖/视频这一级。

    023 原实现按整组聚合，不分 message_type，导致同级继承：一条评论被
    识别出的 SPU 会赋给同组其余所有评论。实测单组最多注入 196 条，
    全库 437 组注入 3,109 条。029 加上层级过滤后注入降到 1,750 条。
    """
    text = (pathlib.Path(__file__).resolve().parents[1]
            / "sql" / "029_inherit_from_root_only.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE FUNCTION voc_backfill_social_spu_inheritance" in text
    assert "m.message_type IN ('帖子', '视频')" in text
    # 事实列绝不能被推断列覆写，023 的这条约束必须保留
    assert "绝不用它覆写事实列" in text


def test_023_flat_group_inheritance_is_superseded() -> None:
    """023 的组级平铺定义仍在文件里（迁移不可改写历史），但 029 必须在其后。"""
    sql_dir = pathlib.Path(__file__).resolve().parents[1] / "sql"
    old = (sql_dir / "023_social_thread_columns.sql").read_text(encoding="utf-8")
    assert "voc_backfill_social_spu_inheritance" in old
    assert (sql_dir / "029_inherit_from_root_only.sql").exists()
