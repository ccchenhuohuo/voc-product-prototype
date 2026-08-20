#!/usr/bin/env python3
"""G4 统一价值门的纯内存回归；不连库、不联网、不调真正 LLM。"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from voc_analytics import config as C, llm, prompts  # noqa: E402
from voc_analytics.stages import value_gate  # noqa: E402


class _Ctx:
    def __init__(self) -> None:
        self.metrics: dict = {}
        self.failed = 0

    def bump(self, *, failed: int = 0, **_kw) -> None:
        self.failed += failed

    def metric_update(self, path, **kw) -> None:
        node = self.metrics
        for part in path:
            node = node.setdefault(part, {})
        node.update(kw)


def _row(message_id: str, content: str, **values: object) -> dict:
    return {
        "message_id": message_id,
        "seq": 0,
        "content": content,
        "message_type": "帖子",
        "platform": "offline",
        "source_requires_spu": False,
        "spu": [],
        "spu_inherited": [],
        **values,
    }


def _run(monkeypatch, rows: list[dict], responder, *, cached=None):
    prompts_seen: list[str] = []
    saved: list[dict] = []

    def fake_chat(prompt: str, **kwargs):
        prompts_seen.append(prompt)
        return responder(prompt, kwargs), {"tokens": 7}

    monkeypatch.setattr(value_gate.llm, "chat_json", fake_chat)
    monkeypatch.setattr(
        value_gate.llm, "parallel_map",
        lambda fn, items, **_kwargs: [fn(item) for item in items],
    )
    result = value_gate.apply_value_gate(
        rows,
        _Ctx(),
        cache_loader=lambda _ids, _ver: list(cached or []),
        cache_saver=lambda values: saved.extend(values) or len(values),
    )
    return result, prompts_seen, saved


_NEGATIVE_CASES = [
    ("So no way to use it with a mirrorless camera? 😅", "（无）"),
    ("分体线会不会测光不灵？", "（无）"),
    ("Does it support ECC RAM?", "（无）"),
    ("太大了有点笨重", "铁头pocket4p保护壳到了"),
    ("一个麦对应一个无线接收器，那做对话博客得插两个？", "amaran Mic 全新上市"),
    ("直接把补光灯砸坏了，还有任何可能的补救方法吗？", "（无）"),
    ("usb网卡很难用，usb3.0会严重干扰2.4G wifi", "（无）"),
    ("你换个小米或者华为的充电器试试", "（无）"),
]

_POSITIVE_CASES = [
    ("什么时候出横杆？！", "诉求缺口", "需要横杆产品"),
    ("能不能出个白色的", "诉求缺口", "需要白色版本产品"),
    ("把MA66L磁吸背板处加上1/4螺口", "诉求缺口", "需要MA66L磁吸背板增加1/4螺口"),
    ("你们的自拍杆21很便携但不能俯拍，85能俯拍又没接口，44都有却只有1.5长度…能不能用点心", "诉求缺口", "需要自拍杆同时具备俯拍与接口"),
    ("买的ulanzi相机三脚架使用第三次就断裂", "产品缺陷", ""),
    ("D100h摁键太灵敏，手扶上去总误触", "产品缺陷", ""),
    ("我的也鼓包了", "产品缺陷", ""),
]


def test_taskbook_examples_all_flow_through_the_single_gate(monkeypatch) -> None:
    monkeypatch.setattr(C, "GATE_VOTE_ENABLED", False)
    rows: list[dict] = []
    expected: dict[str, tuple[str, str]] = {}
    for index, (content, parent_title) in enumerate(_NEGATIVE_CASES):
        mid = f"n{index}"
        rows.append(_row(
            mid, content,
            message_type="评论" if parent_title != "（无）" else "帖子",
            parent_title=parent_title,
        ))
        expected[content] = ("无价值", "")
    for index, (content, cls, claim) in enumerate(_POSITIVE_CASES):
        mid = f"p{index}"
        rows.append(_row(
            mid, content,
            message_type="评论" if content == "我的也鼓包了" else "帖子",
            parent_title=("ULANZI 电池使用反馈"
                          if content == "我的也鼓包了" else "（无）"),
        ))
        expected[content] = (cls, claim)

    def responder(prompt: str, _kwargs: dict) -> dict:
        content = next(text for text in expected if f"【用户内容】{text}" in prompt)
        cls, claim = expected[content]
        return {"cls": cls, "confidence": 0.95, "reason": "offline", "claim": claim}

    result, seen, _saved = _run(monkeypatch, rows, responder)

    assert len(seen) == len(rows)
    assert len(result.no_value_rows) == len(_NEGATIVE_CASES)
    assert len(result.generic_claim_rows) == 0
    assert len(result.passed_rows) == len(_POSITIVE_CASES)
    assert all("硬规则（逐条核对" in prompt for prompt in seen)
    assert any("铁头pocket4p保护壳到了" in prompt for prompt in seen)
    assert any("ULANZI 电池使用反馈" in prompt for prompt in seen)


def test_german_and_vietnamese_rows_reach_llm_without_keyword_prefilter(monkeypatch) -> None:
    monkeypatch.setattr(C, "GATE_VOTE_ENABLED", False)
    rows = [
        _row("de", "Wird es eine Version für die A7M4 geben?"),
        _row("vi", "Có thể làm loại kẹp cả điện thoại không?"),
    ]

    result, seen, _saved = _run(
        monkeypatch,
        rows,
        lambda _prompt, _kwargs: {
            "cls": "诉求缺口", "confidence": 0.95,
            "reason": "offline", "claim": "需要适配对应对象的配件",
        },
    )

    assert len(result.passed_rows) == 2
    assert len(seen) == 2
    assert "A7M4" in seen[0]
    assert "kẹp cả điện thoại" in seen[1]


def test_low_confidence_triggers_two_more_votes_and_uses_majority(monkeypatch) -> None:
    monkeypatch.setattr(C, "GATE_VOTE_ENABLED", True)
    replies = iter([
        {"cls": "诉求缺口", "confidence": 0.5, "reason": "first", "claim": "需要横杆"},
        {"cls": "无价值", "confidence": 0.9, "reason": "second", "claim": ""},
        {"cls": "无价值", "confidence": 0.8, "reason": "third", "claim": ""},
    ])
    result, seen, saved = _run(
        monkeypatch, [_row("m1", "横杆吗")],
        lambda _prompt, _kwargs: next(replies),
    )

    assert len(seen) == 3
    assert len(result.no_value_rows) == 1
    assert saved[0]["cls"] == "无价值"
    assert saved[0]["votes"] == 3


def test_first_product_defect_vote_always_triggers_three_votes(monkeypatch) -> None:
    monkeypatch.setattr(C, "GATE_VOTE_ENABLED", True)
    result, seen, saved = _run(
        monkeypatch, [_row("m1", "三脚架断了", spu=["S1"])],
        lambda _prompt, _kwargs: {
            "cls": "产品缺陷", "confidence": 0.99,
            "reason": "offline", "claim": "",
        },
    )

    assert len(seen) == 3
    assert len(result.passed_rows) == 1
    assert saved[0]["votes"] == 3


def test_cache_hit_skips_llm_and_expands_to_all_message_rows(monkeypatch) -> None:
    monkeypatch.setattr(
        value_gate.llm, "parallel_map",
        lambda *_args, **_kwargs: pytest.fail("缓存命中不应送 LLM"),
    )
    rows = [_row("m1", "能不能出白色", seq=0),
            _row("m1", "能不能出白色", seq=1)]
    cached = [{
        "message_id": "m1", "cls": "诉求缺口", "claim": "需要白色版本产品",
        "confidence": 0.91, "votes": 1, "prompt_ver": prompts.VALUE_GATE_VER,
    }]

    result = value_gate.apply_value_gate(
        rows, _Ctx(), cache_loader=lambda _ids, _ver: cached,
        cache_saver=lambda _rows: pytest.fail("缓存命中不应回写"),
    )

    assert len(result.passed_rows) == 2
    assert result.stats["cache_hit_messages"] == 1
    assert all(row["_gate_cached"] for row in result.passed_rows)


@pytest.mark.parametrize("claim", ["", "需要某款待发布新品", "缺少某个配件能力"])
def test_generic_claim_is_an_explicit_terminal_state(monkeypatch, claim: str) -> None:
    monkeypatch.setattr(C, "GATE_VOTE_ENABLED", False)
    result, _seen, _saved = _run(
        monkeypatch, [_row("m1", "希望出个新的")],
        lambda _prompt, _kwargs: {
            "cls": "诉求缺口", "confidence": 0.95,
            "reason": "offline", "claim": claim,
        },
    )
    assert len(result.generic_claim_rows) == 1
    assert result.passed_rows == []


def test_parent_title_assembly_comment_post_and_missing_root() -> None:
    root = _row(
        "root", "帖子正文", message_group_id="g1",
        message_type="帖子", message_title="ULANZI 三脚架使用反馈",
    )
    comment = _row(
        "comment", "我的也断了", message_group_id="g1",
        message_type="评论",
    )
    missing = _row(
        "missing", "我的也断了", message_group_id="g2",
        message_type="回复",
    )

    assert value_gate.parent_title_for(comment, [root, comment]) == "ULANZI 三脚架使用反馈"
    assert value_gate.parent_title_for(root, [root, comment]) == "（无）"
    assert value_gate.parent_title_for(missing, [root, missing]) == "（无）"


def test_nonfatal_single_message_failure_is_accounted(monkeypatch) -> None:
    monkeypatch.setattr(C, "GATE_VOTE_ENABLED", False)
    ctx = _Ctx()
    monkeypatch.setattr(
        value_gate.llm, "parallel_map",
        lambda fn, items, **_kwargs: [fn(item) for item in items],
    )
    monkeypatch.setattr(
        value_gate.llm, "chat_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(llm.LLMError("offline")),
    )

    result = value_gate.apply_value_gate(
        [_row("m1", "消息")], ctx,
        cache_loader=lambda _ids, _ver: [], cache_saver=lambda _rows: 0,
    )

    assert len(result.failed_rows) == 1
    assert result.stats["failed_messages"] == 1
    assert ctx.failed == 1
