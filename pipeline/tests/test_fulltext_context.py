"""片段+完整原文的上下文契约。

背景：云听把一条评论按标签切成单句片段，Stage1/Stage2 此前只读片段，
信息不足时模型只能编（52 例 A/B：只喂片段硬伤率 17%，补全文后 0%）。
本文件锁三件事：生成池必须携带 full_text；提示词渲染必须带上完整原文
且同一消息只带一次；整段情感必须随消息入库。
"""
from __future__ import annotations

import re

from voc_analytics import clean, db
from voc_analytics.stages import stage1


def test_generation_pool_carries_full_text() -> None:
    """生成池 SQL 必须投影 full_text，译文优先、原文兜底。"""
    import inspect

    src = inspect.getsource(db.generation_pool)
    assert "AS full_text" in src
    assert re.search(r"content_zh[^\n]*\n[^\n]*content[^\n]*\)\s*,\s*400\)", src), (
        "full_text 应为 COALESCE(content_zh, content) 且截断 400 字"
        "——与 _evidence_body 的 limit 是同一契约值"
    )


def _item(mid: str, snippet: str, full: str | None) -> dict:
    return {"message_id": mid, "snippet": snippet, "full_text": full}


def test_fmt_items_appends_full_text() -> None:
    rendered = stage1._fmt_items(
        [_item("m1", "正规大品牌的", "自拍杆的遥控器，快改进，连最基本的对焦功能都没有")],
        [0])
    assert "正规大品牌的" in rendered
    assert "【完整原文】自拍杆的遥控器" in rendered


def test_fmt_items_dedupes_full_text_per_message() -> None:
    """同一消息的多个片段只带一次全文——实测单消息最多被切 8 片，
    重复携带会把同一段话喂 8 遍。"""
    full = "自拍杆的遥控器，快改进，连最基本的对焦功能都没有"
    rendered = stage1._fmt_items(
        [_item("m1", "正规大品牌的", full), _item("m1", "快改进", full),
         _item("m2", "杆子有点粗", "杆子有点粗，收纳也不方便")],
        [0, 1, 2])
    assert rendered.count("【完整原文】") == 2          # m1 一次 + m2 一次
    assert "【完整原文：同上条消息】" in rendered      # m1 的第二个片段
    assert rendered.count(full) == 1


def test_fmt_items_skips_full_text_when_identical() -> None:
    """片段就是全文（电商短评常态）时不重复渲染。"""
    rendered = stage1._fmt_items([_item("m1", "会掉漆", "会掉漆")], [0])
    assert "【完整原文】" not in rendered


def test_fmt_items_without_full_text_keeps_old_shape() -> None:
    """无 full_text 键（旧数据回放/夹具）时保持原渲染，不抛错。"""
    rendered = stage1._fmt_items([{"message_id": "m1", "snippet": "会掉漆"}], [0])
    assert rendered.endswith("会掉漆")


def test_to_message_maps_msg_sentiment() -> None:
    row = {"消息ID": "s1", "评论内容": "整段在抱怨", "消息情感": "负面"}
    msg = clean.to_message(row, "社媒", "b1", set())
    assert msg["msg_sentiment"] == "负面"


def test_to_message_msg_sentiment_absent_is_none() -> None:
    """导出缺该列（历史文件回放）时安全为 None，不阻断入库。"""
    row = {"消息ID": "s2", "评论内容": "x"}
    msg = clean.to_message(row, "电商", "b1", set())
    assert msg["msg_sentiment"] is None


def test_generate_renderer_carries_full_text() -> None:
    """Stage2 标题与 Stage4 复核用 generate._fmt_items——它曾是一份
    不带【完整原文】的旧副本，让全文改造只生效一半（2026-08-19 评审抓出）。
    锁死：该渲染器必须与 Stage1 输出同一 body。"""
    from voc_analytics.stages import generate

    rendered = generate._fmt_items(
        [_item("m1", "正规大品牌的", "自拍杆的遥控器，快改进，连最基本的对焦功能都没有")],
        [0])
    assert "【完整原文】自拍杆的遥控器" in rendered


def test_generate_has_no_private_body_builder() -> None:
    """禁止 generate.py 再长出自己的 body 实现——重复实现正是上一个 bug。"""
    import inspect

    from voc_analytics.stages import generate, stage1

    assert generate._evidence_body is stage1._evidence_body
    src = inspect.getsource(generate)
    assert "def _evidence_text" not in src


def test_ensure_message_schema_names_migration() -> None:
    """缺列必须在拉数之前失败，且报错指向迁移编号。"""
    import inspect

    from voc_analytics import db as _db, ingest as _ingest

    assert "ensure_message_schema" in inspect.getsource(_ingest.ingest_window)
    src = inspect.getsource(_db.ensure_message_schema)
    assert "027" in src
