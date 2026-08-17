#!/usr/bin/env python3
"""用量分账与费用估算的回归；不连库、不联网。

历史缺陷：_USAGE 只记 total_tokens，既分不出输入/输出（单价差 2.5 倍），
也分不出 embedding 与 chat，导致「这一轮花了多少钱」无法回答。

用法：
  cd pipeline && python3 -m pytest tests/test_cost_accounting.py
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics import config as C, llm  # noqa: E402


class AccountingTest(unittest.TestCase):
    def setUp(self) -> None:
        llm.reset_for_tests()

    def test_input_and_output_are_recorded_separately_per_model(self) -> None:
        llm._account("qwen-plus", {"prompt_tokens": 1000, "completion_tokens": 200,
                                   "total_tokens": 1200})
        llm._account("text-embedding-v4", {"prompt_tokens": 500, "total_tokens": 500})

        snapshot = llm.usage()
        self.assertEqual(snapshot["calls"], 2)
        self.assertEqual(snapshot["tokens"], 1700)
        self.assertEqual(snapshot["by_model"]["qwen-plus"],
                         {"calls": 1, "input": 1000, "output": 200, "tokens": 1200})
        self.assertEqual(snapshot["by_model"]["text-embedding-v4"],
                         {"calls": 1, "input": 500, "output": 0, "tokens": 500})

    def test_total_only_response_counts_as_input(self) -> None:
        """只有 total 时全算输入——宁可低估高单价的输出，也不凭比例猜。"""
        llm._account("qwen-plus", {"total_tokens": 900})
        entry = llm.usage()["by_model"]["qwen-plus"]
        self.assertEqual((entry["input"], entry["output"], entry["tokens"]),
                         (900, 0, 900))

    def test_missing_total_falls_back_to_the_sum(self) -> None:
        llm._account("qwen-plus", {"prompt_tokens": 10, "completion_tokens": 5})
        self.assertEqual(llm.usage()["by_model"]["qwen-plus"]["tokens"], 15)

    def test_malformed_usage_never_raises(self) -> None:
        """计费统计不能反过来把一次成功的业务调用打挂。"""
        for bad in (None, "usage", [], {"prompt_tokens": "many"},
                    {"prompt_tokens": True}):
            llm._account("qwen-plus", bad)
        self.assertEqual(llm.usage()["by_model"]["qwen-plus"]["tokens"], 0)
        self.assertEqual(llm.usage()["calls"], 5)

    def test_usage_snapshot_is_detached_from_live_state(self) -> None:
        llm._account("qwen-plus", {"prompt_tokens": 100, "total_tokens": 100})
        snapshot = llm.usage()
        llm._account("qwen-plus", {"prompt_tokens": 100, "total_tokens": 100})
        self.assertEqual(snapshot["by_model"]["qwen-plus"]["input"], 100)

    def test_reset_clears_the_per_model_ledger(self) -> None:
        llm._account("qwen-plus", {"prompt_tokens": 100, "total_tokens": 100})
        llm.reset_usage()
        self.assertEqual(llm.usage()["by_model"], {})


class CostTest(unittest.TestCase):
    def setUp(self) -> None:
        llm.reset_for_tests()

    def test_cost_uses_direction_specific_prices(self) -> None:
        llm._account("qwen-plus", {"prompt_tokens": 1_000_000,
                                   "completion_tokens": 1_000_000,
                                   "total_tokens": 2_000_000})
        price = C.PRICE_PER_MTOK["qwen-plus"]
        breakdown = llm.cost()
        self.assertAlmostEqual(breakdown["cny"],
                               round(price["input"] + price["output"], 2), places=2)

    def test_embedding_output_is_free(self) -> None:
        llm._account("text-embedding-v4", {"prompt_tokens": 2_000_000,
                                           "total_tokens": 2_000_000})
        expected = 2 * C.PRICE_PER_MTOK["text-embedding-v4"]["input"]
        self.assertAlmostEqual(llm.cost()["cny"], round(expected, 2), places=2)

    def test_unknown_model_is_flagged_not_silently_free(self) -> None:
        llm._account("qwen-experimental", {"prompt_tokens": 1_000_000,
                                           "total_tokens": 1_000_000})
        breakdown = llm.cost()
        self.assertGreater(breakdown["cny"], 0, "未登记的模型被悄悄按 0 计费了")
        self.assertEqual(breakdown["unpriced"], ["qwen-experimental"])
        self.assertFalse(breakdown["by_model"]["qwen-experimental"]["priced"])

    def test_format_cost_mentions_每个模型(self) -> None:
        llm._account("qwen-plus", {"prompt_tokens": 100, "completion_tokens": 10,
                                   "total_tokens": 110})
        llm._account("text-embedding-v4", {"prompt_tokens": 50, "total_tokens": 50})
        line = llm.format_cost()
        self.assertIn("qwen-plus", line)
        self.assertIn("text-embedding-v4", line)
        self.assertIn("¥", line)

    def test_no_calls_is_zero_not_an_error(self) -> None:
        self.assertEqual(llm.cost()["cny"], 0)
        self.assertIn("无调用", llm.format_cost())


if __name__ == "__main__":
    unittest.main()
