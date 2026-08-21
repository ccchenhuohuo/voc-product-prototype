#!/usr/bin/env python3
"""OPP2 身份键的纯函数契约；不连库、不调模型。"""
from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _offline_imports import ensure_psycopg_importable  # noqa: E402

ensure_psycopg_importable()

from voc_analytics import pipeline  # noqa: E402
from voc_analytics.pipeline import make_opp_id  # noqa: E402


def _id_from_evidence(evidence: dict) -> str:
    """模拟两个来源行组装同一语义身份。

    ``src_line`` 故意保留在输入中，但 v3 身份材料只提取生命周期与
    内容语义；这使测试能直接回归“来源不入哈希”。
    """
    return make_opp_id(
        evidence["opp_type"], evidence.get("core_tag"), evidence.get("problem_mode"),
    )


def test_same_old_problem_across_sources_has_one_stable_opp2_id() -> None:
    ecommerce = {
        "src_line": "电商",
        "opp_type": "老品迭代",
        "core_tag": "SPU-A",
        "problem_mode": "长时间使用后-按键回弹失效",
    }
    social = {
        "src_line": "社媒",
        "opp_type": "老品迭代",
        "core_tag": "SPU-A",
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
        "新品创新", "MAGNETIC-Mount", "按键／回弹失效")

    assert old_id == normalized_old_id
    assert innovation_id != old_id


def test_innovation_identity_is_unified_without_source_or_removed_dimensions() -> None:
    base = {
        "src_line": "问卷调研",
        "opp_type": "新品创新",
        "core_tag": None,
        "problem_mode": "希望在弱光时自动补光",
    }
    first = _id_from_evidence(base)
    same_semantics_other_source = _id_from_evidence(
        {**base, "src_line": "社媒"})
    different_mode = _id_from_evidence(
        {**base, "problem_mode": "希望增加自动调光能力"})

    assert first == same_semantics_other_source
    assert first != different_mode
    assert base["core_tag"] is None


def test_build_writes_old_core_as_spu_and_innovation_core_as_null(monkeypatch) -> None:
    mode = "按键在长时间使用后无法正常回弹"
    monkeypatch.setattr(
        pipeline.generate, "write_prototype",
        lambda *_a, **_k: {
            "title": "改善按键长时间使用后的回弹稳定性",
            "problem_mode": mode,
            "desc_phenomenon": "按键长时间使用后不能正常回弹。",
            "desc_attribution": "回弹结构耐久性不足。",
            "safety_flag": False,
            "_meta": {},
        },
    )
    monkeypatch.setattr(
        pipeline.generate, "write_suggestion",
        lambda *_a, **_k: ("验证并提高回弹结构耐久性。", "ctx-hash"),
    )
    monkeypatch.setattr(
        pipeline.generate, "llm_review",
        lambda *_a, **_k: {"ok": True, "issues": [], "soft_issues": []},
    )

    common = {
        "message_id": "m1", "seq": 0, "src_line": "社媒", "star": None,
        "category": "支架类", "country": "CN", "product_name": "样品",
        "source_requires_spu": False, "evidence_text": "按键用久后弹不起来",
    }
    group = {"mode_name": mode, "members": [0]}
    context = {
        "tag": "SPU-A", "prod_line": "支撑", "storage_category": "支架类",
    }

    old = pipeline.build_opportunity(
        [{
            **common, "_opp_type": "老品迭代", "assigned_spu": "SPU-A",
            "assignment_source": "fact", "assign_run_id": "run-v3",
        }],
        group, "老品迭代", context, object(), [],
    )
    innovation = pipeline.build_opportunity(
        [{
            **common, "_opp_type": "新品创新", "assigned_spu": None,
            "assignment_source": None, "assign_run_id": "run-v3",
        }],
        group, "新品创新", {**context, "tag": None}, object(), [],
    )
    innovation_again = pipeline.build_opportunity(
        [{
            **common, "_opp_type": "新品创新", "assigned_spu": None,
            "assignment_source": None, "assign_run_id": "run-v3",
        }],
        group, "新品创新", {**context, "tag": None}, object(), [],
    )

    assert old["core_tag"] == "SPU-A"
    assert innovation["core_tag"] is None
    assert innovation["opp_id"] == innovation_again["opp_id"]
