#!/usr/bin/env python3
"""Stage1/Stage2 提示词由生命周期选择；全程 mock，不调 LLM。"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics.config import RunCtx  # noqa: E402
from voc_analytics.stages import generate, stage1  # noqa: E402


@pytest.mark.parametrize(
    ("opp_type", "expected"),
    [("老品迭代", "按【失效模式】分组"),
     ("新品创新", "按【用户诉求主题】或【对标维度】分组")],
)
def test_stage1_prompt_is_selected_by_lifecycle(
    monkeypatch, opp_type: str, expected: str,
) -> None:
    prompts: list[str] = []

    def fake_chat(prompt: str, **kwargs):
        del kwargs
        prompts.append(prompt)
        return ({"modes": [{"mode_name": "按键卡滞", "evidence_idx": [1]}],
                 "unclassified": []}, {})

    monkeypatch.setattr(stage1.llm, "chat_json", fake_chat)
    result = stage1.split_batch(
        [{"message_id": "m1", "seq": 1, "evidence_text": "按键会卡住"}],
        opp_type, [0], {"bucket_key": "offline"}, 1, 1)

    assert result["modes"] == {"按键卡滞": [0]}
    assert expected in prompts[0]


@pytest.mark.parametrize(
    ("opp_type", "unit"),
    [("老品迭代", "同一个失效模式下"),
     ("新品创新", "同一个诉求主题下")],
)
def test_stage2_prompt_is_selected_by_lifecycle(
    monkeypatch, opp_type: str, unit: str,
) -> None:
    prompts: list[str] = []

    def fake_chat(prompt: str, **kwargs):
        del kwargs
        prompts.append(prompt)
        return ({
            "title": "相机配件：修复按键卡滞",
            "problem_mode": "按键在正常按压时卡住",
            "desc_phenomenon": "有用户反馈按键在正常按压时卡住，无法顺利回弹。",
            "desc_attribution": "推断指向按键结构的回弹稳定性不足。",
        }, {"tokens": 0})

    monkeypatch.setattr(generate.llm, "chat_json", fake_chat)
    monkeypatch.setattr(generate.validate, "validate_stage2", lambda *args: [])
    ctx = RunCtx(run_id="offline-prompt", week="2026-W34")

    generate.write_prototype(
        [{"message_id": "m1", "seq": 1, "evidence_text": "按键会卡住"}],
        [0], "按键卡滞", opp_type, {"category": "相机配件"}, ctx)

    assert f"生命周期是【{opp_type}】" in prompts[0]
    assert unit in prompts[0]
