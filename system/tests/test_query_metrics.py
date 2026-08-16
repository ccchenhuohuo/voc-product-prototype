from math import sqrt

from app import queries as Q
from scripts.check_sql import PARAMS


def compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_queue_and_search_sort_by_wilson_lower_bound():
    for sql in (Q.BOARD_SPUS, Q.SEARCH_SPUS):
        text = compact(sql)
        assert "negative_evi_count::numeric" in text
        assert "nullif(" in text
        assert "3.8416" in text
        assert "1.96 * sqrt" in text
        assert "when r.sample_size = 0 then 0::numeric" in text
        assert "then r.wilson_score else null end" in text
        assert text.endswith("r.wilson_score desc, r.spu")


def _wilson(negative: int, positive: int) -> float:
    total = negative + positive
    if not total:
        return 0.0
    proportion = negative / total
    return (
        proportion + 1.96**2 / (2 * total)
        - 1.96 * sqrt(
            (proportion * (1 - proportion) + 1.96**2 / (4 * total)) / total
        )
    ) / (1 + 1.96**2 / total)


def test_wilson_suppresses_one_item_sample_below_large_sample():
    rows = [("small", 1, 0), ("large", 608, 2093)]
    ordered = sorted(rows, key=lambda row: _wilson(row[1], row[2]), reverse=True)
    assert [row[0] for row in ordered] == ["large", "small"]


def test_two_of_two_example_in_task_has_higher_wilson_score_than_608_of_2701():
    # 任务书要求相反顺序，但按它指定的 z=1.96 公式，0.3424 > 0.2097。
    assert _wilson(2, 0) > _wilson(608, 2093)


def test_pending_total_sort_uses_open_count_then_total_count():
    for sql in (Q.BOARD_SPUS, Q.SEARCH_SPUS):
        text = compact(sql)
        for direction in ("asc", "desc"):
            marker = f"ordering.sort_key = 'issues' and ordering.sort_dir = '{direction}'"
            assert text.count(marker) == 2
        assert "then r.open_issue_count else null end" in text
        assert "then r.issue_count else null end" in text


def test_tag_facets_are_ordered_by_coverage_before_name():
    text = compact(Q.SEARCH_TAG_FACETS)
    assert "order by x.level_order, x.count desc, x.domain, x.sub, x.leaf" in text


def test_pending_over_total_uses_manual_status_with_default():
    for sql in (Q.BOARD_SPUS, Q.SEARCH_SPUS, Q.SPU_DETAIL):
        text = compact(sql)
        assert "from voc_spu_issue i" in text
        assert "left join voc_spu_issue_manual m" in text
        assert "coalesce(m.status, '考虑中')" in text
        assert "('已完成', '不考虑')" in text
        assert "open_issue_count" in text
        assert "issue_count" in text

    count_sections = (
        compact(Q.BOARD_SPUS).split("issue_state as (", 1)[1].split(
            "), issue_counts as", 1
        )[0],
        compact(Q.SEARCH_SPUS).split("issue_counts as (", 1)[1].split(
            "), results as", 1
        )[0],
        compact(Q.SPU_DETAIL).split("issue_counts as (", 1)[1].split(
            ") select s.*", 1
        )[0],
    )
    for section in count_sections:
        assert "voc_opportunity" not in section
        assert "merged_into" not in section


def test_revived_queue_query_matches_the_sidebar_definition():
    text = compact(Q.BOARD_SPUS_REVIVED)
    assert "m.status = '考虑中'" in text
    assert "m.revived_at is not null" in text
    assert "left join voc_spu_issue i on i.spu = m.spu and i.opp_id = m.opp_id" in text
    assert "left join voc_opportunity o on o.opp_id = m.opp_id" in text
    assert "o.merged_into is null" in text
    assert "join revived_spus v on v.spu = c.spu" in text


def test_recent_evidence_uses_publish_time_never_attach_week():
    for sql in (Q.BOARD_SPUS, Q.BOARD_ISSUES, Q.SPU_ISSUES, Q.ISSUE_DETAIL):
        text = compact(sql)
        assert "join voc_message" in text
        assert "publish_time >= now() - interval '60 days'" in text
        assert "attach_week" not in text


def test_manual_only_issue_is_kept_as_history_with_live_evidence_fallback():
    for sql in (Q.SPU_ISSUES, Q.ISSUE_DETAIL):
        text = compact(sql)
        assert "left join voc_spu_issue i" in text
        assert "actual_evi_count" in text
        assert "m.baseline_evi_count" in text
        assert "historical" in text
    assert "union select m.spu, m.opp_id from voc_spu_issue_manual m" in compact(
        Q.SPU_ISSUES
    )


def test_manual_spu_history_cannot_reintroduce_innovations():
    spu_manual_branch = compact(Q.SPU_ISSUES).split("union", 1)[1].split(
        "), evidence_agg as", 1
    )[0]
    assert "o.classification_state = '确定'" in spu_manual_branch
    assert "o.opp_type = '老品迭代'" in spu_manual_branch

    detail_filter = compact(Q.ISSUE_DETAIL).split(" where ", 1)[-1]
    assert "o.classification_state = '确定'" in detail_filter
    assert "o.opp_type = '老品迭代'" in detail_filter

    completed_revival = compact(Q.REVIVE_COMPLETED)
    assert "join voc_opportunity o on o.opp_id = m.opp_id" in completed_revival
    assert "o.classification_state = '确定'" in completed_revival
    assert "o.opp_type = '老品迭代'" in completed_revival


def test_spu_medians_are_computed_from_all_spus():
    text = compact(Q.SPU_DETAIL)
    assert text.count("percentile_cont(0.5)") == 2
    assert "with medians as" in text
    assert "from voc_spu s" in text
    assert text.index("from voc_spu s") < text.rindex("where s.spu = %s")


def test_innovation_rollups_use_all_evidence_without_brand_multiplication():
    for sql in (Q.BOARD_INNOVATIONS, Q.INNOVATION_DETAIL):
        text = compact(sql)
        assert "sum(coalesce(msg.interactions, 0))" in text
        assert "filter (where e.tax_path is not null)" in text
        assert "join lateral" in text
        assert ") x on true" in text
        assert "array_agg(distinct x.brand order by x.brand)" in text
        assert text.index("brand_agg as") > text.index("evidence_agg as")


def test_strategy_counts_distinct_spus():
    assert "count(distinct i.spu)" in compact(Q.STRATEGY_OPPORTUNITIES)


def test_opportunity_boards_exclude_invalid_classifications():
    for sql in (
        Q.SHELL_COUNTS,
        Q.BOARD_INNOVATIONS,
        Q.SPU_ISSUES,
        Q.ISSUE_DETAIL,
        Q.INNOVATION_DETAIL,
        Q.STRATEGY_OPPORTUNITIES,
    ):
        assert "classification_state = '确定'" in compact(sql)


def test_new_read_queries_use_explicit_join_conditions():
    for sql in (
        Q.SHELL_COUNTS,
        Q.BOARD_SPUS,
        Q.BOARD_SPUS_REVIVED,
        Q.BOARD_ISSUES,
        Q.BOARD_INNOVATIONS,
        Q.SEARCH_SPUS,
        Q.SEARCH_SPUS_BY_DOMAIN,
        Q.SEARCH_SPUS_BY_SUB,
        Q.SEARCH_SPUS_BY_LEAF,
        Q.SPU_DETAIL,
        Q.SPU_ISSUES,
        Q.ISSUE_DETAIL,
        Q.ISSUE_VOICES,
        Q.INNOVATION_DETAIL,
        Q.INNOVATION_EVIDENCE,
        Q.STRATEGY_OPPORTUNITIES,
        Q.SEARCH_TAG_FACETS,
    ):
        assert " using " not in compact(sql)


def test_every_public_query_is_registered_with_matching_parameter_count():
    names = {
        name for name in dir(Q)
        if name.isupper() and isinstance(getattr(Q, name), str)
    }
    assert names == set(PARAMS)
    for name in names:
        assert getattr(Q, name).count("%s") == len(PARAMS[name]), name
