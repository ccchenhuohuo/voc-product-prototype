#!/usr/bin/env python3
"""分类前移与生命周期路由的纯函数测试；不连库、不联网。"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics.classification import Classification, classify_evidence  # noqa: E402
from voc_analytics.routing import (  # noqa: E402
    LifecycleBucket,
    route_evidence_by_lifecycle,
)


class LifecycleRoutingTest(unittest.TestCase):
    def test_same_old_product_topic_across_sources_shares_one_bucket(self) -> None:
        ecommerce = {
            "src_line": "电商",
            "source_requires_spu": True,
            "spu": ["SPU-A"],
            "tag": "  MAGNETIC   Mount ",
        }
        social = {
            "src_line": "社媒",
            "source_requires_spu": False,
            "spu": ["SPU-A"],
            "topic": "ＭＡＧＮＥＴＩＣ mount",
        }

        result = route_evidence_by_lifecycle([ecommerce, social])

        expected_bucket = LifecycleBucket("老品迭代", "magnetic mount", None)
        self.assertEqual(list(result.buckets), [expected_bucket])
        self.assertEqual(len(result.buckets[expected_bucket]), 2)
        self.assertEqual(result.invalid, [])
        self.assertNotIn("电商", repr(expected_bucket))
        self.assertNotIn("社媒", repr(expected_bucket))
        self.assertTrue(
            all(
                row["_classification"]
                == Classification("老品迭代", "确定", "R1")
                for row in result.buckets[expected_bucket]
            )
        )
        self.assertTrue(
            all(row["_opp_type"] == "老品迭代"
                for row in result.buckets[expected_bucket])
        )
        self.assertTrue(
            all(row["_classification_state"] == "确定"
                for row in result.buckets[expected_bucket])
        )
        self.assertTrue(
            all(row["_classify_rule"] == "R1"
                for row in result.buckets[expected_bucket])
        )
        self.assertNotIn("_classification", ecommerce)
        self.assertNotIn("_classification", social)

    def test_ecommerce_request_is_old_with_spu_and_invalid_without_spu(self) -> None:
        with_spu = {
            "src_line": "电商",
            "source_requires_spu": True,
            "spu": ["SPU-A"],
            "tag": "电量显示",
        }
        without_spu = {
            "src_line": "电商",
            "source_requires_spu": True,
            "spu": [],
            "tag": "电量显示",
        }

        result = route_evidence_by_lifecycle([with_spu, without_spu])

        self.assertIn(LifecycleBucket("老品迭代", "电量显示", None), result.buckets)
        self.assertEqual(len(result.invalid), 1)
        self.assertEqual(result.invalid[0]["_classification"].classify_rule, "R3")
        # 固定规则下不存在「电商新品」：有 SPU 是 R1，无 SPU 是 R3。
        self.assertNotIn("新品创新", {b.opp_type for b in result.buckets})

    def test_social_user_experience_routes_by_spu_mount(self) -> None:
        common = {
            "src_line": "社媒",
            "source_requires_spu": False,
            "content_branch": "用户使用体验",
            "channel": "产品体验",
        }
        with_spu = {**common, "spu": ["SPU-A"], "topic": "按键卡滞"}
        without_spu = {**common, "spu": [], "topic": "按键卡滞"}

        result = route_evidence_by_lifecycle([with_spu, without_spu])

        self.assertIn(LifecycleBucket("老品迭代", "按键卡滞", None), result.buckets)
        self.assertIn(
            LifecycleBucket("新品创新", "用户使用体验", "产品体验"),
            result.buckets,
        )
        self.assertEqual(result.invalid, [])

    def test_same_text_without_spu_social_stays_innovation_by_fixed_r2(self) -> None:
        """显式固化需求书的规则冲突：语义相同不能跨生命周期。"""
        ecommerce = {
            "src_line": "电商", "source_requires_spu": True,
            "spu": ["SPU-A"], "tag": "按键卡滞",
        }
        social = {
            "src_line": "社媒", "source_requires_spu": False,
            "spu": [], "topic": "按键卡滞",
            "content_branch": "用户使用体验", "channel": "产品体验",
        }

        result = route_evidence_by_lifecycle([ecommerce, social])

        self.assertIn(LifecycleBucket("老品迭代", "按键卡滞", None), result.buckets)
        self.assertIn(
            LifecycleBucket("新品创新", "用户使用体验", "产品体验"),
            result.buckets,
        )
        self.assertEqual(
            {bucket.opp_type for bucket in result.buckets},
            {"老品迭代", "新品创新"},
        )

    def test_survey_without_spu_is_innovation_without_source_name_branching(self) -> None:
        survey = {
            "src_line": "问卷调研",
            "source_requires_spu": False,
            "spu": None,
            "channel": "开放题",
            "content_branch": "新品需求",
        }

        result = route_evidence_by_lifecycle([survey])

        bucket = LifecycleBucket("新品创新", "新品需求", "开放题")
        self.assertEqual(list(result.buckets), [bucket])
        self.assertEqual(result.buckets[bucket][0]["_classification"].classify_rule, "R2")
        self.assertEqual(result.invalid, [])

    def test_zero_evidence_has_no_generation_or_invalid_rows(self) -> None:
        self.assertEqual(classify_evidence([]), Classification(None, "无效", "R0"))
        result = route_evidence_by_lifecycle([])
        self.assertEqual(result.buckets, {})
        self.assertEqual(result.invalid, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
