from datetime import datetime, timezone

from app.viewmodels import (
    group_board,
    negative_ratio,
    normalize_strategy_rows,
    sort_issues,
    sort_spus,
)


def test_spu_order_uses_negative_ratio_not_absolute_volume_or_grade():
    rows = [
        {"spu": "large", "grade": "PS级", "negative_evi_count": 500,
         "positive_evi_count": 4500},
        {"spu": "small", "grade": "D级", "negative_evi_count": 30,
         "positive_evi_count": 70},
    ]
    assert [row["spu"] for row in sort_spus(rows)] == ["small", "large"]


def test_negative_ratio_handles_empty_denominator():
    assert negative_ratio({"negative_evi_count": 0, "positive_evi_count": 0}) == 0


def test_revived_issue_is_pinned_first():
    rows = [
        {"opp_id": "popular", "evi_count": 100, "status": "考虑中", "tax_path": None},
        {"opp_id": "revived", "evi_count": 2, "status": "考虑中", "tax_path": None,
         "revived_at": datetime.now(timezone.utc)},
    ]
    assert sort_issues(rows)[0]["opp_id"] == "revived"


def test_completed_issue_sinks_to_bottom():
    rows = [
        {"opp_id": "done", "evi_count": 100, "status": "已完成", "tax_path": None},
        {"opp_id": "active", "evi_count": 2, "status": "考虑中", "tax_path": None},
    ]
    assert sort_issues(rows)[-1]["opp_id"] == "done"


def test_dismissed_issue_also_sinks_to_bottom():
    rows = [
        {"opp_id": "drop", "evi_count": 100, "status": "不考虑", "tax_path": None},
        {"opp_id": "active", "evi_count": 2, "status": "考虑中", "tax_path": None},
    ]
    assert sort_issues(rows)[-1]["opp_id"] == "drop"


def test_previously_revived_but_completed_issue_still_sinks():
    rows = [
        {"opp_id": "done", "evi_count": 100, "status": "已完成", "tax_path": None,
         "revived_at": datetime.now(timezone.utc)},
        {"opp_id": "active", "evi_count": 2, "status": "考虑中", "tax_path": None},
    ]
    assert sort_issues(rows)[-1]["opp_id"] == "done"


def test_board_excludes_spu_without_issues():
    spus = [
        {"spu": "with", "grade": "A", "negative_evi_count": 2,
         "positive_evi_count": 8},
        {"spu": "empty", "grade": "PS", "negative_evi_count": 4,
         "positive_evi_count": 6},
    ]
    issues = [{"spu": "with", "opp_id": "o1", "evi_count": 2,
               "status": "考虑中", "tax_path": None}]
    assert [card["spu"] for card in group_board(spus, issues)] == ["with"]


def test_strategy_bars_use_current_result_maximum():
    rows = normalize_strategy_rows([
        {"opp_id": "a", "spu_count": 2},
        {"opp_id": "b", "spu_count": 7},
    ])
    assert rows[0]["spu_bar_pct"] == 2 / 7 * 100
    assert rows[1]["spu_bar_pct"] == 100

    filtered = normalize_strategy_rows([{"opp_id": "a", "spu_count": 2}])
    assert filtered[0]["spu_bar_pct"] == 100
