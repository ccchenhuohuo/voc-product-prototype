"""原声「正句 + 完整原声展开」契约。

方案：片段作正句（现状不变），full_content 存在时折叠展开完整原文；
片段能在全文中逐字定位时拆 pre/hit/post 供 <mark> 高亮，定位不到
（外语片段 vs 中译文，实测社媒占一半）就整段展开不高亮。
"""
from __future__ import annotations

from pathlib import Path

from app import queries as Q
from app.viewmodels import attach_full_voice, attach_message_highlights

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_issue_voices_query_ships_full_content() -> None:
    assert "full_content" in Q.ISSUE_VOICES
    # 仅当正句确为片段且原文另有内容时才下发，防止整段回落时重复展示。
    assert "IS DISTINCT FROM btrim(e.snippet)" in Q.ISSUE_VOICES


def test_attach_full_voice_partitions_on_exact_hit() -> None:
    rows = [{"voice_text": "杆子有点粗",
             "full_content": "到手试了下，杆子有点粗，收纳不便。"}]
    out = attach_full_voice(rows)
    assert out[0]["full_pre"] == "到手试了下，"
    assert out[0]["full_hit"] == "杆子有点粗"
    assert out[0]["full_post"] == "，收纳不便。"


def test_attach_full_voice_no_hit_keeps_plain_expand() -> None:
    """外语片段配中译全文：定位不到，保留 full_content、不产出高亮三段。"""
    rows = [{"voice_text": "se weno pero muy bajito",
             "full_content": "看起来不错，但对我来说太矮了。"}]
    out = attach_full_voice(rows)
    assert "full_hit" not in out[0]
    assert out[0]["full_content"]


def test_attach_full_voice_missing_full_is_noop() -> None:
    rows = [{"voice_text": "会掉漆", "full_content": None}]
    out = attach_full_voice(rows)
    assert "full_hit" not in out[0]


def test_templates_render_inline_original() -> None:
    """老品保留切片+原文；新品直接显示完整 message 与可选译文。"""
    issue = (TEMPLATES / "issue-voices.html").read_text(encoding="utf-8")
    inno = (TEMPLATES / "innovation-card.html").read_text(encoding="utf-8")
    assert "full-voice" in issue and "原文：" in issue and "中文：" in issue
    assert "<mark>" in issue and "<details" not in issue
    assert "row.content_parts" in inno and "中文：" in inno
    assert "<mark>" in inno and "<details" not in inno
    assert issue.index("voice.voice_text") < issue.index("原文：") < issue.index("中文：")


def test_voice_text_trims_whitespace_snippets() -> None:
    """空白片段的空判必须与 full_content 一致（btrim），否则页面渲染
    一行空白且拿不到展开——评审抓出的边界。"""
    assert "COALESCE(NULLIF(btrim(e.snippet), ''), msg.content)" in Q.ISSUE_VOICES
    assert "'snippet', e.snippet" in Q.INNOVATION_EVIDENCE


def test_innovation_template_renders_full_messages_in_scroll_window() -> None:
    """新品原声按 message 展示全量，不再把前三个证据片段当三条原声。"""
    tpl = (TEMPLATES / "innovation-card.html").read_text(encoding="utf-8")
    assert "inspiration-window" in tpl
    assert "evidence[:3]" not in tpl
    assert "row.content_parts" in tpl
    assert "row.translation_parts" in tpl


def test_innovation_query_groups_by_message_and_keeps_all_hits() -> None:
    sql = Q.INNOVATION_EVIDENCE.lower()
    assert "group by oe.message_id" in sql
    assert "jsonb_agg" in sql and "hit_count" in sql
    assert "limit 3" not in sql


def test_message_highlights_multiple_unique_and_overlapping_snippets() -> None:
    rows = [{
        "message_id": "M1",
        "content": "你们动作太慢了，适配 Luna 的产品别家都早就上市了。",
        "content_zh": None,
        "hit_count": 4,
        "hits": [
            {"snippet": "你们动作太慢了", "snippet_raw": "你们动作太慢了"},
            {"snippet": "动作太慢", "snippet_raw": None},
            {"snippet": "适配 luna 的产品别家都早就上市了", "snippet_raw": None},
            {"snippet": "你们动作太慢了", "snippet_raw": None},
        ],
    }]
    item = attach_message_highlights(rows)[0]
    highlighted = [part["text"] for part in item["content_parts"]
                   if part["highlighted"]]
    assert highlighted == ["你们动作太慢了", "适配 Luna 的产品别家都早就上市了"]
    assert item["hit_count"] == 4


def test_message_highlights_translation_when_snippet_is_not_in_original() -> None:
    rows = [{
        "content": "Need that for my Luna!",
        "content_zh": "我需要露娜的那个配件！",
        "hits": [{"snippet": "露娜的那个配件", "snippet_raw": None}],
    }]
    item = attach_message_highlights(rows)[0]
    assert not any(part["highlighted"] for part in item["content_parts"])
    assert [part["text"] for part in item["translation_parts"]
            if part["highlighted"]] == ["露娜的那个配件"]


def test_issue_voices_uses_the_frozen_relation_assignment() -> None:
    """卡片与原声必须共享关系行归属，不能从可变消息数组二次推导。"""
    assert "oe.assigned_spu = %s" in Q.ISSUE_VOICES
    assert "msg.spu" not in Q.ISSUE_VOICES


def test_no_query_matches_fact_spu_alone() -> None:
    """全局守卫：v3 消费 SQL 不得再读消息 SPU 数组。"""
    source = __import__("pathlib").Path(Q.__file__).read_text(encoding="utf-8")
    assert "ANY(COALESCE(msg.spu" not in source
    assert "ANY(COALESCE(m.spu" not in source
    assert "msg.spu_inherited" not in source
    assert "m.spu_inherited" not in source
