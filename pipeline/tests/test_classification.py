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
            {"src_line": "社媒", "spu": []},
            {"src_line": "社媒", "spu": ["SPU-A"]},
        ]
        self.assertEqual(
            classify_evidence(evidence),
            Classification("老品迭代", "确定", "R1"),
        )

    def test_r1_evidence_can_span_multiple_spus(self) -> None:
        evidence = [
            {"src_line": "社媒", "spu": ["SPU-A", "SPU-B"]},
            {"src_line": "电商", "spu": ["SPU-C"]},
        ]
        self.assertEqual(classify_evidence(evidence).classify_rule, "R1")
        self.assertEqual(classify_evidence(evidence).opp_type, "老品迭代")

    def test_r2_social_without_spu_is_innovation(self) -> None:
        result = classify_evidence([{"src_line": "社媒", "spu": []}])
        self.assertEqual(result, Classification("新品创新", "确定", "R2"))

    def test_r2_precedes_r3_for_mixed_sources_without_spu(self) -> None:
        evidence = [
            {"src_line": "电商", "spu": []},
            {"src_line": "社媒", "spu": None},
        ]
        expected = Classification("新品创新", "确定", "R2")
        self.assertEqual(classify_evidence(evidence), expected)
        self.assertEqual(classify_evidence(reversed(evidence)), expected)

    def test_r3_ecommerce_without_spu_is_invalid(self) -> None:
        result = classify_evidence([{"src_line": "电商", "spu": []}])
        self.assertEqual(result, Classification(None, "无效", "R3"))

    def test_spu_must_be_an_array_not_a_string(self) -> None:
        with self.assertRaisesRegex(ValueError, "spu 必须是数组"):
            classify_evidence([{"src_line": "社媒", "spu": "SPU-A"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
