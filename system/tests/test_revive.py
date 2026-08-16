from datetime import date

from scripts.check_revive import detect_revivals


def test_dismissed_issue_revives_when_evidence_triples():
    rows = [{"spu": "S1", "opp_id": "O1", "baseline_evi_count": 2,
             "current_evi_count": 6}]
    result = detect_revivals(rows, [])
    assert [(r.trigger, r.spu, r.opp_id) for r in result] == [("evidence_tripled", "S1", "O1")]


def test_completed_issue_revives_on_post_release_feedback():
    rows = [{"spu": "S2", "opp_id": "O2", "release_date": date(2026, 6, 20),
             "target_release": "v2.1", "new_feedback_count": 4}]
    result = detect_revivals([], rows)
    assert result[0].trigger == "post_release_feedback"
    assert "上市后仍有 4 条新反馈" in result[0].reason
