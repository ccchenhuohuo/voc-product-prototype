#!/usr/bin/env python3
"""产品字段清洗的纯函数回归测试；不连库、不联网。

用法：
  cd pipeline && python3 -m pytest tests/test_clean_fields.py
  # 也可：cd pipeline && python3 tests/test_clean_fields.py

前置条件：
  Python 3.11+；pytest 用法还需安装项目测试依赖。无需环境变量或外部服务。
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics import clean  # noqa: E402
from voc_analytics.taxonomy import split_tax  # noqa: E402


class CleanProductFieldsTest(unittest.TestCase):
    def test_multi_value_fields_use_declared_semantics(self) -> None:
        row = {
            "消息ID": "real-shape-1",
            "品类_": "[S支架类, T三脚架类]",
            "品名_": "[maglock 小小吸盘支架, 海外别名]",
            "产品定级_": "[A级, B级]",
            "SPU_": "[G00A16, G00A17]",
            "SKU_": "[A053, A054]",
            "型号_": "[MA30, MA30 Pro]",
            "模型_": "[支撑, 套装]",
            "新品上市时间_": "[2024Q1, 2024Q2]",
            "本竞品_": "[竞品, 本品]",
        }

        message = clean.to_message(row, "电商", "batch-1", set())

        self.assertEqual(message["product_grade"], "A级")
        self.assertEqual(message["spu"], ["G00A16", "G00A17"])
        self.assertEqual(message["sku"], ["A053", "A054"])
        self.assertEqual(message["model"], ["MA30", "MA30 Pro"])
        self.assertEqual(message["category"], "S支架类")
        self.assertEqual(message["product_name"], "maglock 小小吸盘支架")
        self.assertEqual(message["prod_line"], "支撑")
        self.assertEqual(message["launch_period"], "2024Q1")
        self.assertTrue(message["is_own_brand"])

    def test_grade_priority_handles_unbracketed_export(self) -> None:
        self.assertEqual(clean.highest_grade("D级, PS级, 其他, A级"), "PS级")
        self.assertEqual(clean.highest_grade("其他, B级, S级"), "S级")

    def test_single_values_do_not_regress(self) -> None:
        row = {
            "消息ID": "real-shape-2",
            "品类_": "[灯光类]",
            "品名_": "补光灯 L1",
            "产品定级_": "C级",
            "SPU_": "[L001]",
            "SKU_": "[]",
            "型号_": None,
            "模型_": "灯光",
            "新品上市时间_": "2023及以前",
            "本竞品_": "竞品",
        }

        message = clean.to_message(row, "电商", "batch-2", set())

        self.assertEqual(message["category"], "灯光类")
        self.assertEqual(message["product_name"], "补光灯 L1")
        self.assertEqual(message["product_grade"], "C级")
        self.assertEqual(message["spu"], ["L001"])
        self.assertEqual(message["sku"], [])
        self.assertEqual(message["model"], [])
        self.assertIs(message["is_own_brand"], False)

    def test_unlabelled_brand_stays_unknown(self) -> None:
        """社媒约九成没有本竞品归属，缺失必须是 NULL 而不是 false，
        否则本品筛选会把「不知道」当「竞品」静默丢掉。"""
        self.assertIsNone(clean.own_brand(None))
        self.assertIsNone(clean.own_brand("[]"))
        self.assertIs(clean.own_brand("本品"), True)
        self.assertIs(clean.own_brand("竞品"), False)

    def test_split_tax_matches_sql_positions_and_missing_levels(self) -> None:
        self.assertEqual(
            split_tax("支撑-全局分析/A4.Act/产品体验/产品部件/配件"),
            ("A4.Act", "产品体验", "产品部件", "配件"),
        )
        self.assertEqual(
            split_tax("支撑-全局分析/A4.Act//产品部件"),
            ("A4.Act", None, "产品部件", None),
        )
        self.assertEqual(split_tax(None), (None, None, None, None))

    def test_evidence_rows_receive_tax_columns(self) -> None:
        class FakeTaxonomy:
            def info(self, tag: str) -> dict:
                return {
                    "tag": tag,
                    "tax_path": "支撑-全局分析/A4.Act/产品体验/产品部件/配件",
                    "tax_l1": "支撑-全局分析",
                    "is_product": True,
                    "is_scene": False,
                }

        evidence, misaligned = clean.explode({
            "消息ID": "real-shape-3",
            "全局标签": "[快装配件]",
            "标签情感,与全局标签一一对应": "[负面]",
            "标签原声片段,与全局标签一一对应": "[配件容易松脱]",
            "评论星级": 2,
        }, FakeTaxonomy(), "电商")

        self.assertEqual(misaligned, 0)
        self.assertEqual(
            tuple(evidence[0][key] for key in
                  ("tax_stage", "tax_domain", "tax_sub", "tax_leaf")),
            ("A4.Act", "产品体验", "产品部件", "配件"),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
