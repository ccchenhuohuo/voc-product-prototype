#!/usr/bin/env python3
"""原声片段里 JSON 空值哨兵的清洗回归；不连库、不联网。

云听把 JSON 空值原样打进平行数组（`[null, 支架松动, null]`），parse_array
按逗号切开后得到字符串 "null"。它非空、非空白，所以
NULLIF(btrim(snippet), '') 挡不住，COALESCE(snippet, content) 也不回落，
证据正文一路以四个字母的 "null" 进到提示词。2026-08-17 实测 148,934 条
证据里 23,608 条中招，「需求缺口」渠道 64% 的入池行受影响。

用法：
  cd pipeline && python3 -m pytest tests/test_null_sentinel.py
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics import clean  # noqa: E402


class NullTokenTest(unittest.TestCase):
    def test_sentinels_become_none_case_insensitively(self) -> None:
        for raw in ("null", "NULL", " Null ", "none", "NaN", "undefined", "nil",
                    "", "   ", None):
            self.assertIsNone(clean.null_token(raw), f"未识别哨兵：{raw!r}")

    def test_real_text_survives_untouched(self) -> None:
        # 含哨兵子串的正常文本不能被误杀。
        for raw in ("支架松动", "nullable 字段没填", "The null hypothesis", "0", "-"):
            self.assertEqual(clean.null_token(raw), raw.strip())


class ExplodeAlignmentTest(unittest.TestCase):
    """置空绝不能变成删元素——三个平行数组按下标一一对应。"""

    def _row(self) -> dict:
        return {
            "消息ID": "align-1",
            "全局标签": "[磁吸, 支架稳定性, 续航]",
            "标签情感,与全局标签一一对应": "[负面, 负面, 正面]",
            "标签原声片段,与全局标签一一对应": "[null, 云台一直晃, null]",
            "评论星级": "2",
        }

    def test_sentinel_snippet_does_not_shift_later_evidence(self) -> None:
        class _Tax:
            def info(self, raw_tag: str) -> dict:
                return {"tag": raw_tag, "tax_path": "", "tax_l1": "",
                        "is_product": True, "is_scene": False}

        rows, misaligned = clean.explode(self._row(), _Tax(), "社媒")

        self.assertEqual(misaligned, 0)
        self.assertEqual(len(rows), 3, "元素被删掉了，后续标签会整体错位")
        self.assertEqual([r["seq"] for r in rows], [0, 1, 2])
        self.assertEqual([r["tag"] for r in rows],
                         ["磁吸", "支架稳定性", "续航"])
        # 哨兵位置置空，真实片段仍然停在原来的下标上。
        self.assertIsNone(rows[0]["snippet"])
        self.assertIsNone(rows[0]["snippet_raw"])
        self.assertEqual(rows[1]["snippet"], "云台一直晃")
        self.assertIsNone(rows[2]["snippet"])
        # 情感没有跟着片段一起错位。
        self.assertEqual([r["sentiment"] for r in rows], ["负面", "负面", "正面"])

    def test_sentinel_is_stripped_before_it_reaches_snippet_raw(self) -> None:
        """留痕列记的是「没有片段」，不是「片段的内容是 null」。"""
        class _Tax:
            def info(self, raw_tag: str) -> dict:
                return {"tag": raw_tag, "tax_path": "", "tax_l1": "",
                        "is_product": False, "is_scene": False}

        rows, _ = clean.explode(
            {"消息ID": "raw-1", "全局标签": "[磁吸]",
             "标签情感,与全局标签一一对应": "[中性]",
             "标签原声片段,与全局标签一一对应": "[null]"}, _Tax(), "社媒")

        self.assertIsNone(rows[0]["snippet_raw"])


if __name__ == "__main__":
    unittest.main()
