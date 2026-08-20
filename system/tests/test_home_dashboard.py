from __future__ import annotations

from pathlib import Path

from app import queries as Q


ROOT = Path(__file__).resolve().parents[2]


def compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_home_v2_replaces_all_old_home_queries():
    names = {name for name in dir(Q) if name.startswith("HOME")}
    assert names == {
        "HOME2_SOURCES", "HOME2_FLOW_SOCIAL", "HOME2_FLOW_EC",
        "HOME2_STATUS", "HOME2_FRESHNESS", "HOME2_WEEKLY", "HOME2_DIST",
    }


def test_social_unique_attribution_priority_and_message_level_dedup_are_in_sql():
    sql = compact(Q.HOME2_FLOW_SOCIAL)
    assigned = sql.split("assigned as materialized (", 1)[1].split(
        "), lifecycle_by_message", 1
    )[0]
    priority = (
        "用户咨询", "用户使用体验", "产品评测", "竞品拉踩",
        "其他", "二手转让", "产品种草广告",
    )
    positions = [assigned.index(f"array['{label}']") for label in priority]
    assert positions == sorted(positions)
    assert "select distinct oe.message_id, oe.opp_id" in sql
    assert "bool_or(o.opp_type = '老品迭代')" in sql
    assert "bool_or(o.opp_type = '新品创新')" in sql


def test_ecommerce_uses_smallest_evidence_seq_then_dynamic_top_six():
    sql = compact(Q.HOME2_FLOW_EC)
    assert "select distinct on (e.message_id)" in sql
    assert "order by e.message_id, e.seq" in sql
    assert "r.tag_rank <= 6" in sql
    assert "r.tag_rank > 6" in sql
    assert "'其余 ' || c.remaining_class_count::text || ' 类'" in sql
    assert "coalesce(p.label, '无标签')" in sql


def test_status_defaults_missing_manual_rows_to_considering():
    sql = compact(Q.HOME2_STATUS)
    assert sql.count("coalesce(m.status, '考虑中')") >= 4
    assert "from voc_spu_issue i" in sql
    assert "where o.opp_type = '新品创新'" in sql
    for status in ("考虑中", "在跟进", "项目中", "已完成", "不考虑"):
        assert status in Q.HOME2_STATUS


def test_freshness_checks_both_dangling_caches_and_weekly_fills_gaps():
    freshness = compact(Q.HOME2_FRESHNESS)
    assert "from voc_spu_issue i" in freshness
    assert "from voc_opp_nn n" in freshness
    assert "left join voc_opportunity a on a.opp_id = n.opp_id" in freshness
    assert "left join voc_opportunity b on b.opp_id = n.neighbor_id" in freshness
    assert "i.issue_dangling + n.nn_dangling" in freshness

    weekly = compact(Q.HOME2_WEEKLY)
    assert "generate_series" in weekly
    assert "interval '25 weeks'" in weekly
    assert "to_char(w.week_start, 'iyyy-\"w\"iw')" in weekly


def test_distribution_is_parameterized_and_groups_top_eight_plus_other():
    sql = compact(Q.HOME2_DIST)
    assert sql.count("%s") == 2
    assert "r.item_rank <= 8" in sql
    assert "r.item_rank > 8" in sql
    assert "coalesce(nullif(btrim(m.lang), ''), '未标注')" in sql
    assert "coalesce(nullif(btrim(m.platform), ''), '未标注')" in sql


def test_probe_migration_and_script_keep_requests_off_the_home_path():
    migration = (ROOT / "pipeline/sql/022_source_probe_cache.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS voc_source_probe" in migration
    assert "GRANT SELECT ON voc_source_probe TO voc_human, voc_reader" in migration
    assert "has_table_privilege('voc_human', 'voc_source_probe', 'SELECT')" in migration

    script = (ROOT / "pipeline/scripts/probe_source_totals.py").read_text()
    for source in ("COMMENT", "SOCIAL", "SERVICE"):
        assert f'("{source}"' in script
    assert "_create_task(" in script
    assert "_cloud_time(start), _cloud_time(end), 1, topic_filter" in script
    assert '"matched": int(result.get("matched_count", 0))' in script
    assert "db.upsert(" in script


def test_tokens_define_every_home_color_in_all_three_theme_blocks():
    tokens = (ROOT / "system/app/static/_tokens.css").read_text()
    for token in ("iter", "inno", "idle", "s1", "s2", "s3", "s4", "s5"):
        assert tokens.count(f"--{token}:") == 3


def test_old_home_css_and_runtime_gantt_are_gone():
    css = (ROOT / "system/app/static/app.css").read_text()
    template = (ROOT / "system/app/templates/home.html").read_text()
    for old in (
        ".home-section", ".home-chart", ".home-fill", ".home-panel",
        "证据漏斗", "语义相似度分布", "数据新鲜度与运行健康",
    ):
        assert old not in css
        assert old not in template
    assert "管道运行" not in template


def test_local_htmx_runtime_supports_get_filters_and_parameter_hook():
    runtime = (ROOT / "system/app/static/htmx.min.js").read_text()
    assert 'closest("[hx-get]")' in runtime
    assert '"htmx:configRequest"' in runtime
    assert "targetUrl.searchParams.set" in runtime
    assert 'element.getAttribute("hx-get"),"GET"' in runtime
