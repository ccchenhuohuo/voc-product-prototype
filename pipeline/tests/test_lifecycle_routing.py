#!/usr/bin/env python3
"""G5 2×2 与统一生命周期分桶的纯函数测试。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics.classification import Classification, classify_evidence  # noqa: E402
from voc_analytics.routing import (  # noqa: E402
    LifecycleBucket,
    classify_evidence_by_lifecycle,
    route_classified_evidence,
    route_social_value_evidence,
)


def _social(value_cls: str, *, spu=None, inherited=None, **values: object) -> dict:
    assignments = [
        {"assigned_spu": value, "assignment_source": "fact"}
        for value in (spu or [])
    ] + [
        {"assigned_spu": value, "assignment_source": "root"}
        for value in (inherited or [])
    ]
    return {
        "message_id": values.pop("message_id", "m1"),
        "seq": values.pop("seq", 0),
        "src_line": "社媒",
        "source_requires_spu": False,
        "assign_run_id": "run-v3",
        "spu_assignments": assignments,
        "_value_cls": value_cls,
        "tag": "按键卡滞",
        "_claim": "需要可俯拍的自拍杆",
        **values,
    }


def test_request_with_spu_enters_existing_product_pool() -> None:
    routed = route_social_value_evidence([
        _social("诉求缺口", spu=["SPU-A"]),
    ])

    assert routed.unassigned_defects == []
    assert routed.eligible[0]["_classification"] == Classification(
        "老品迭代", "确定", "R1")
    buckets = route_classified_evidence(routed.eligible).buckets
    assert list(buckets) == [LifecycleBucket("老品迭代", "SPU-A")]


def test_request_without_spu_enters_innovation_precluster_pool() -> None:
    routed = route_social_value_evidence([
        _social("诉求缺口", _precluster_id="PCL-ABC123"),
    ])

    assert routed.unassigned_defects == []
    assert routed.eligible[0]["_classification"] == Classification(
        "新品创新", "确定", "R2")
    buckets = route_classified_evidence(routed.eligible).buckets
    assert list(buckets) == [LifecycleBucket("新品创新", "pcl-abc123")]


def test_product_defect_with_inherited_spu_enters_existing_product_pool() -> None:
    routed = route_social_value_evidence([
        _social("产品缺陷", inherited=["SPU-INHERITED"]),
    ])

    assert routed.unassigned_defects == []
    assert routed.eligible[0]["_opp_type"] == "老品迭代"
    assert routed.eligible[0]["_classify_rule"] == "R1"


def test_product_defect_without_spu_is_terminal_unassigned() -> None:
    routed = route_social_value_evidence([_social("产品缺陷")])

    assert routed.eligible == []
    assert len(routed.unassigned_defects) == 1
    assert routed.unassigned_defects[0]["_terminal"] == "缺陷不可归属"


def test_same_old_product_tag_across_sources_shares_one_bucket() -> None:
    ecommerce = {
        "src_line": "电商", "source_requires_spu": True,
        "assign_run_id": "run-v3",
        "spu_assignments": [
            {"assigned_spu": "SPU-A", "assignment_source": "fact"},
        ],
        "tag": "  MAGNETIC   Mount ",
    }
    social = _social(
        "产品缺陷", inherited=["SPU-A"], tag="ＭＡＧＮＥＴＩＣ mount",
    )
    social_routed = route_social_value_evidence([social]).eligible
    ecommerce_classified, invalid = classify_evidence_by_lifecycle([ecommerce])
    assert invalid == []

    result = route_classified_evidence([*ecommerce_classified, *social_routed])

    expected = LifecycleBucket("老品迭代", "SPU-A")
    assert list(result.buckets) == [expected]
    assert len(result.buckets[expected]) == 2


def test_zero_evidence_classification_remains_r0() -> None:
    assert classify_evidence([]) == Classification(None, "无效", "R0")
    assert route_classified_evidence([]).buckets == {}


def test_claim_with_inherited_only_spu_is_demoted_to_innovation() -> None:
    """诉求 + SPU 仅来自组继承 → 降级为无 SPU，走新品创新。

    继承的语义是「这条评论所在的帖子在讲哪个产品」，不是「这条评论在讲
    哪个产品」。实测抖音组 douyin-7622225804197856241 共 50 条评论、
    只有 1 条被识别出 A200，其余 49 条继承后把「能不能出个小卡收纳盒」
    这类新品诉求灌进了 A200 的老品问题池。
    """
    routed = route_social_value_evidence([
        _social("诉求缺口", inherited=["SPU-A"], _precluster_id="PCL-ABC123"),
    ])

    assert routed.unassigned_defects == []
    assert routed.eligible[0]["_classification"] == Classification(
        "新品创新", "确定", "R2")
    assert routed.eligible[0]["_inherit_demoted"] is True
    assert routed.eligible[0]["spu_assignments"] == []


def test_claim_with_fact_spu_still_enters_existing_product_pool() -> None:
    """只降级继承来的。云听从正文直接识别出 SPU 的诉求仍进老品——
    用户明确对着某个产品提要求，归属没有疑问。"""
    routed = route_social_value_evidence([
        _social("诉求缺口", spu=["SPU-A"]),
    ])

    assert routed.eligible[0]["_classification"] == Classification(
        "老品迭代", "确定", "R1")
    assert "_inherit_demoted" not in routed.eligible[0]


def test_claim_with_fact_and_root_keeps_only_fact_assignment() -> None:
    routed = route_social_value_evidence([
        _social("诉求缺口", spu=["SPU-A"], inherited=["SPU-B"]),
    ])

    assert [(row["assigned_spu"], row["assignment_source"])
            for row in routed.eligible] == [("SPU-A", "fact")]


def test_multi_spu_snapshot_fans_out_once_per_spu() -> None:
    classified, invalid = classify_evidence_by_lifecycle([
        {
            "message_id": "multi", "seq": 1, "src_line": "电商",
            "source_requires_spu": True, "assign_run_id": "run-v3",
            "spu_assignments": [
                {"assigned_spu": "SPU-B", "assignment_source": "root"},
                {"assigned_spu": "SPU-A", "assignment_source": "fact"},
                {"assigned_spu": "SPU-B", "assignment_source": "fact"},
            ],
        }
    ])
    routed = route_classified_evidence(classified)

    assert invalid == []
    assert set(routed.buckets) == {
        LifecycleBucket("老品迭代", "SPU-A"),
        LifecycleBucket("老品迭代", "SPU-B"),
    }
    assert sum(map(len, routed.buckets.values())) == 2
    by_spu = {row["assigned_spu"]: row["assignment_source"]
              for rows in routed.buckets.values() for row in rows}
    assert by_spu == {"SPU-A": "fact", "SPU-B": "fact"}


def test_defect_with_inherited_spu_is_not_demoted() -> None:
    """降级只作用于诉求。缺陷继承 SPU 仍进老品——它在报某个产品的故障，
    而组继承给出的产品归属是当前唯一线索；砍掉会让它落进「缺陷不可归属」
    终态被丢弃，那是净损失。"""
    routed = route_social_value_evidence([
        _social("产品缺陷", inherited=["SPU-A"]),
    ])

    assert routed.unassigned_defects == []
    assert routed.eligible[0]["_classification"] == Classification(
        "老品迭代", "确定", "R1")
    assert "_inherit_demoted" not in routed.eligible[0]
