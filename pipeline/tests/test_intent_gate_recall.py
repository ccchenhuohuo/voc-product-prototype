#!/usr/bin/env python3
"""诉求门召回口径的回归；不连库、不联网（LLM 用桩替换）。

历史缺陷：LLM 之前有一道中文关键词正则预筛，只有命中的行才送判。
本渠道 7,914 行里 4,082 行不是中文，德/法/俄/越/意/印尼语实测命中率
全部为 0.00%，整个语种的诉求信号被静默清零。2026-08-17 实测通过率 0.62%。

用法：
  cd pipeline && python3 -m pytest tests/test_intent_gate_recall.py
"""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics import pipeline  # noqa: E402


class _Ctx:
    def __init__(self) -> None:
        self.metrics: dict = {}

    def bump(self, **_kw) -> None:
        pass

    def metric_update(self, path, **kw) -> None:
        self.metrics.setdefault(path, {}).update(kw)


class IntentTextTest(unittest.TestCase):
    def test_snippet_content_and_translation_are_all_offered(self) -> None:
        text = pipeline._intent_text({
            "evidence_text": "lo práctico que ess",
            "snippet": "lo práctico que ess",
            "content": "¿dónde puedo comprarlo?",
            "content_zh": "我在哪儿能买到啊",
        })
        self.assertIn("lo práctico", text)
        self.assertIn("dónde puedo", text)
        self.assertIn("我在哪儿能买到", text)
        # 同一段文本重复出现在多列时不重复拼接。
        self.assertEqual(text.count("lo práctico que ess"), 1)

    def test_text_is_capped(self) -> None:
        self.assertLessEqual(len(pipeline._intent_text({"content": "啊" * 5000})), 800)

    def test_row_without_any_text_yields_empty(self) -> None:
        self.assertEqual(pipeline._intent_text({"snippet": "", "content": None}), "")


class IntentGateRecallTest(unittest.TestCase):
    """非中文诉求必须能进 LLM，不能在正则那一层就被清零。"""

    ROWS = [
        {"content": "Wird es eine Version für die A7M4 geben?", "lang": "德文"},
        {"content": "Có thể làm loại kẹp cả điện thoại không?", "lang": "越南语"},
        {"content": "能不能出个白色的", "lang": "中文"},
    ]

    def test_every_row_with_text_reaches_the_classifier(self) -> None:
        seen: list[str] = []

        def fake_chat_json(prompt, **_kw):
            seen.append(prompt)
            return {"intent": "需求缺口", "confidence": 0.9}, {"tokens": 10}

        original_chat, original_map = (pipeline.llm.chat_json,
                                       pipeline.llm.parallel_map)
        pipeline.llm.chat_json = fake_chat_json
        pipeline.llm.parallel_map = lambda fn, items, **kw: [fn(i) for i in items]
        try:
            out, stats = pipeline.intent_gate(list(self.ROWS), _Ctx())
        finally:
            pipeline.llm.chat_json = original_chat
            pipeline.llm.parallel_map = original_map

        self.assertEqual(len(seen), 3, "有行没送到 LLM——预筛又回来了")
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["no_text"], 0)
        self.assertEqual(len(out), 3)
        self.assertIn("A7M4", seen[0])
        self.assertIn("kẹp cả điện thoại", seen[1])

    def test_low_confidence_passes_but_is_marked_for_review(self) -> None:
        def fake_chat_json(_prompt, **_kw):
            return {"intent": "需求缺口", "confidence": 0.5}, {"tokens": 10}

        original_chat, original_map = (pipeline.llm.chat_json,
                                       pipeline.llm.parallel_map)
        pipeline.llm.chat_json = fake_chat_json
        pipeline.llm.parallel_map = lambda fn, items, **kw: [fn(i) for i in items]
        try:
            out, _ = pipeline.intent_gate([{"content": "希望出个新的"}], _Ctx())
        finally:
            pipeline.llm.chat_json = original_chat
            pipeline.llm.parallel_map = original_map

        self.assertEqual(len(out), 1, "0.4 <= conf < 0.6 应放行（R11）")
        self.assertTrue(out[0]["_needs_review"])

    def test_non_gap_intents_are_dropped(self) -> None:
        def fake_chat_json(_prompt, **_kw):
            return {"intent": "找货", "confidence": 0.95}, {"tokens": 10}

        original_chat, original_map = (pipeline.llm.chat_json,
                                       pipeline.llm.parallel_map)
        pipeline.llm.chat_json = fake_chat_json
        pipeline.llm.parallel_map = lambda fn, items, **kw: [fn(i) for i in items]
        try:
            out, stats = pipeline.intent_gate([{"content": "多少钱"}], _Ctx())
        finally:
            pipeline.llm.chat_json = original_chat
            pipeline.llm.parallel_map = original_map

        self.assertEqual(out, [])
        self.assertEqual(stats["intent"], {"找货": 1})


if __name__ == "__main__":
    unittest.main()
