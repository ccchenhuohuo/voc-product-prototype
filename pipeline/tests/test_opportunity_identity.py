#!/usr/bin/env python3
"""OPP2 身份键的纯函数契约；不连库、不调模型。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics.pipeline import make_opp_id  # noqa: E402


def _id_from_evidence(evidence: dict) -> str:
    """模拟两个来源行组装同一语义身份。

    ``src_line`` 故意保留在输入中，但 v2 身份材料只提取生命周期与
    内容语义；这使测试能直接回归“来源不入哈希”。
    """
    return make_opp_id(
        evidence["opp_type"], evidence.get("core_tag"), evidence.get("problem_mode"),
        evidence.get("channel"),
    )


def test_same_old_problem_across_sources_has_one_stable_opp2_id() -> None:
    ecommerce = {
        "src_line": "电商",
        "opp_type": "老品迭代",
        "core_tag": "按键卡滞",
        "problem_mode": "长时间使用后-按键回弹失效",
    }
    social = {
        "src_line": "社媒",
        "opp_type": "老品迭代",
        "core_tag": "按键卡滞",
        "problem_mode": "  长时间使用后，按键回弹失效。 ",
    }

    first = _id_from_evidence(ecommerce)
    second = _id_from_evidence(social)

    assert first == second
    assert first == _id_from_evidence(ecommerce)
    assert first.startswith("OPP2-")
    assert len(first) == len("OPP2-") + 32


def test_identity_normalization_is_stable_but_lifecycle_isolated() -> None:
    old_id = make_opp_id("老品迭代", "MAGNETIC-Mount", "按键／回弹失效")
    normalized_old_id = make_opp_id("老品迭代", "ＭＡＧＮＥＴＩＣ mount", "按键 回弹失效")
    innovation_id = make_opp_id(
        "新品创新", "MAGNETIC-Mount", "按键／回弹失效", "需求缺口")

    assert old_id == normalized_old_id
    assert innovation_id != old_id


def test_innovation_identity_keeps_l1_channels_isolated_without_source() -> None:
    base = {
        "src_line": "问卷调研",
        "opp_type": "新品创新",
        "core_tag": "便携补光",
        "problem_mode": "希望在弱光时自动补光",
    }
    gap = _id_from_evidence({**base, "channel": "需求缺口"})
    comparison = _id_from_evidence({**base, "channel": "竞品对标"})
    same_gap_other_source = _id_from_evidence(
        {**base, "src_line": "社媒", "channel": "需求缺口"})

    assert gap != comparison
    assert gap == same_gap_other_source
