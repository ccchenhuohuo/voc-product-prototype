#!/usr/bin/env python3
"""关键迁移的静态契约；只读文件，绝不连库或执行 SQL。"""
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


def test_030_defines_exact_union_has_spu_and_reclassifies_existing_rows() -> None:
    text = (SQL / "030_has_spu.sql").read_text(encoding="utf-8")

    assert "CREATE OR REPLACE FUNCTION voc_has_spu(p_message_id text)" in text
    assert "RETURNS boolean LANGUAGE sql STABLE PARALLEL SAFE" in text
    assert "COALESCE(cardinality(m.spu), 0)" in text
    assert "+ COALESCE(cardinality(m.spu_inherited), 0) > 0" in text
    assert "UPDATE public.voc_opportunity" in text
    for rule in ("'R0'", "'R1'", "'R2'", "'R3'"):
        assert rule in text
    assert "opposite_rows" in text and "RAISE EXCEPTION" in text
    assert "classification_mismatch_rows" in text
    assert "重分类后仍有 % 行口径相反" in text


def test_031_adds_immutable_snapshot_projection_scope_and_id_map() -> None:
    text = (SQL / "031_assign_snapshot.sql").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS public.voc_assign_snapshot" in text
    assert "PRIMARY KEY (run_id, message_id, seq, assigned_spu)" in text
    assert "CHECK (source IN ('fact', 'root'))" in text
    assert "BEFORE UPDATE ON public.voc_assign_snapshot" in text
    assert "REFERENCING OLD TABLE AS deleted_rows" in text
    assert "只允许按 run_id 整轮 DELETE" in text
    assert "REVOKE INSERT, UPDATE, TRUNCATE ON public.voc_assign_snapshot" in text
    assert "GRANT SELECT, DELETE ON public.voc_assign_snapshot TO voc_writer" in text
    assert "GRANT SELECT, INSERT, DELETE ON public.voc_assign_snapshot" not in text
    for column in (
        "assigned_spu text", "assignment_source text", "assign_run_id text",
        "scope_source text", "denominator_scope text",
    ):
        assert f"ADD COLUMN IF NOT EXISTS {column}" in text
    assert "ck_oe_assignment_source" in text
    assert "ck_oe_assignment_pair" in text
    assert "ck_oe_assignment_run" in text
    assert "DROP INDEX IF EXISTS public.ix_opp_bucket" in text
    assert "ON public.voc_opportunity (core_tag, opp_type, category)" in text
    assert "CREATE TABLE IF NOT EXISTS public.voc_opp_id_map" in text
    assert "BEFORE UPDATE OR DELETE ON public.voc_opp_id_map" in text


def test_032_builds_two_materializations_and_never_refreshes_the_view() -> None:
    text = (SQL / "032_spu_issue_v2v3.sql").read_text(encoding="utf-8")

    assert "CREATE MATERIALIZED VIEW public.voc_spu_issue_v2 AS" in text
    assert "CREATE MATERIALIZED VIEW public.voc_spu_issue_v3 AS" in text
    assert "CREATE VIEW public.voc_spu_issue AS" in text
    assert "FROM public.voc_spu_issue_v2" in text
    assert "oe.assigned_spu" in text
    assert "oe.assigned_spu = o.core_tag" in text
    assert "o.opp_id LIKE 'OPP-%'" in text
    assert "o.opp_id LIKE 'OPP2-%'" in text
    assert "REFRESH MATERIALIZED VIEW public.voc_spu;" in text
    assert "REFRESH MATERIALIZED VIEW public.voc_spu_issue_v2;" in text
    assert "REFRESH MATERIALIZED VIEW public.voc_spu_issue_v3;" in text
    assert "REFRESH MATERIALIZED VIEW public.voc_spu_issue;" not in text
    assert "v2_signature IS DISTINCT FROM v3_signature" in text
    assert "v2_signature IS DISTINCT FROM compat_signature" in text
    # shadow NN 刷新只替换本代，旧代缓存不得被 TRUNCATE。
    replacement = text[text.index(
        "CREATE OR REPLACE FUNCTION public.voc_refresh_opp_nn()") :]
    assert "DELETE FROM public.voc_opp_nn" in replacement
    assert "WHERE opp_id LIKE 'OPP2-%'" in replacement
    assert "TRUNCATE public.voc_opp_nn" not in replacement


def test_033_snapshot_prepare_is_single_writer_idempotent_and_fingerprinted() -> None:
    text = (SQL / "033_snapshot_prepare.sql").read_text(encoding="utf-8")

    assert "voc_prepare_assign_snapshot(p_run_id text)" in text
    assert "pg_advisory_xact_lock(hashtext(p_run_id))" in text
    assert "SELECT count(*) INTO existing_rows" in text
    assert "IF has_snapshot_log THEN" in text
    assert "RETURN row_count;" in text
    assert "已有归属快照与原指纹不一致" in text
    assert "已有 % 行快照但没有原始指纹" in text
    assert "ON CONFLICT (run_id, message_id, seq, assigned_spu) DO NOTHING" in text
    assert "count(DISTINCT (message_id, seq))" in text
    assert "md5(COALESCE(" in text
    assert "content_fingerprint" in text
    assert "p_run_id || ':assign_snapshot'" in text
    assert "voc_verify_assign_snapshot(p_run_id text)" in text
    assert "IS DISTINCT FROM" in text


def test_035_preserves_legacy_rows_and_adds_run_spu_terminal_grain() -> None:
    text = (SQL / "035_terminal_ledger.sql").read_text(encoding="utf-8")

    assert "BEGIN;" in text and "COMMIT;" in text
    assert "SET LOCAL search_path = public, pg_temp" in text
    assert "035 必须以 voc_admin 执行" in text
    assert "ADD COLUMN IF NOT EXISTS terminal_id bigint" in text
    assert "ADD COLUMN IF NOT EXISTS run_id text" in text
    assert "ADD COLUMN IF NOT EXISTS assigned_spu text" in text
    assert "PRIMARY KEY (terminal_id)" in text
    assert "UNIQUE (run_id, message_id, seq, assigned_spu)" in text
    assert "WHERE run_id IS NULL" in text
    for reason in (
        "unclassified", "vote_dropped", "truncated",
        "generation_failed", "grounding_rejected",
    ):
        assert f"'{reason}'" in text
    assert "DELETE FROM public.voc_unclassified_evidence" not in text
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" in text
    assert "TO voc_writer" in text
    assert "TO voc_human, voc_reader" in text
