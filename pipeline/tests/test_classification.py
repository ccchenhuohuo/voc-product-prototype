#!/usr/bin/env python3
"""机会点分类契约的纯函数测试；不连库、不联网。"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics.classification import Classification, classify_evidence  # noqa: E402


class ClassificationTest(unittest.TestCase):
    def test_r0_zero_evidence_is_invalid(self) -> None:
        self.assertEqual(classify_evidence([]), Classification(None, "无效", "R0"))

    def test_r1_any_spu_makes_existing_product_iteration(self) -> None:
        evidence = [
            {"source_requires_spu": False, "spu": []},
            {"source_requires_spu": False, "spu": ["SPU-A"]},
        ]
        self.assertEqual(
            classify_evidence(evidence),
            Classification("老品迭代", "确定", "R1"),
        )

    def test_r1_evidence_can_span_multiple_spus(self) -> None:
        evidence = [
            {"source_requires_spu": False, "spu": ["SPU-A", "SPU-B"]},
            {"source_requires_spu": True, "spu": ["SPU-C"]},
        ]
        self.assertEqual(classify_evidence(evidence).classify_rule, "R1")
        self.assertEqual(classify_evidence(evidence).opp_type, "老品迭代")

    def test_r1_accepts_inherited_spu_without_mutating_fact_spu(self) -> None:
        evidence = [{
            "source_requires_spu": False,
            "spu": [],
            "spu_inherited": ["SPU-INHERITED"],
        }]
        self.assertEqual(
            classify_evidence(evidence),
            Classification("老品迭代", "确定", "R1"),
        )
        self.assertEqual(evidence[0]["spu"], [])

    def test_has_spu_four_array_states_match_sql_contract(self) -> None:
        cases = (
            (["SPU-FACT"], [], "R1"),
            ([], ["SPU-ROOT"], "R1"),
            (["SPU-FACT"], ["SPU-ROOT"], "R1"),
            ([], [], "R2"),
        )
        for fact, root, expected_rule in cases:
            with self.subTest(fact=fact, root=root):
                result = classify_evidence([{
                    "source_requires_spu": False,
                    "spu": fact,
                    "spu_inherited": root,
                }])
                self.assertEqual(result.classify_rule, expected_rule)

    def test_r2_social_without_spu_is_innovation(self) -> None:
        result = classify_evidence(
            [{"source_requires_spu": False, "spu": []}]
        )
        self.assertEqual(result, Classification("新品创新", "确定", "R2"))

    def test_r2_precedes_r3_for_mixed_sources_without_spu(self) -> None:
        evidence = [
            {"source_requires_spu": True, "spu": []},
            {"source_requires_spu": False, "spu": None},
        ]
        expected = Classification("新品创新", "确定", "R2")
        self.assertEqual(classify_evidence(evidence), expected)
        self.assertEqual(classify_evidence(reversed(evidence)), expected)

    def test_r3_ecommerce_without_spu_is_invalid(self) -> None:
        result = classify_evidence(
            [{"source_requires_spu": True, "spu": []}]
        )
        self.assertEqual(result, Classification(None, "无效", "R3"))

    def test_spu_must_be_an_array_not_a_string(self) -> None:
        with self.assertRaisesRegex(ValueError, "spu 必须是数组"):
            classify_evidence(
                [{"source_requires_spu": False, "spu": "SPU-A"}]
            )

    def test_inherited_spu_has_the_same_array_type_contract(self) -> None:
        with self.assertRaisesRegex(ValueError, "spu_inherited 必须是数组"):
            classify_evidence([{
                "source_requires_spu": False,
                "spu": [],
                "spu_inherited": "SPU-A",
            }])

    def test_source_policy_is_required_even_when_spu_is_present(self) -> None:
        with self.assertRaisesRegex(ValueError, "缺少 source_requires_spu"):
            classify_evidence([{"spu": ["SPU-A"]}])

    def test_source_policy_must_be_a_real_boolean(self) -> None:
        with self.assertRaisesRegex(ValueError, "source_requires_spu 必须是 bool"):
            classify_evidence([{"source_requires_spu": 0, "spu": []}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
