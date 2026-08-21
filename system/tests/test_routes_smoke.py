from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.routes import (architecture, board, innovation, issue, search, spu,
                        strategy)


SPU = {
    "spu": "SPU-1",
    "product_names": ["测试产品"],
    "skus": ["SKU-1"],
    "category": "支架类",
    "prod_line": "支撑",
    "grade": "A级",
    "launch_period": "2026Q1",
    "has_ec": True,
    "message_count": 12,
    "negative_evi_count": 3,
    "positive_evi_count": 7,
    "negative_ratio": 0.3,
    "avg_star": 3.9,
    "open_issue_count": 1,
    "issue_count": 1,
    "recent_evi_count": 2,
    "median_negative_ratio": 0.16,
    "median_avg_star": 4.3,
}

ISSUE = {
    "spu": "SPU-1",
    "opp_id": "OPP-1",
    "title": "车载支架易松动",
    "evi_count": 4,
    "recent_evi_count": 2,
    "status": "考虑中",
    "revived_at": None,
    "n_eff": 1.7,
    "scope": "多品",
    "tax_domain": "稳定性",
    "tax_sub": "固定",
    "tax_leaf": "松动",
    "product_names": ["测试产品"],
    "voice_avg_star": 3.8,
}

INNO = {
    "opp_id": "INNO-1",
    "title": "新场景创新需求",
    "core_tag": "户外",
    "status": "考虑中",
    "evi_total": 3,
    "evi_social": 3,
    "interactions": 45,
    "brands": ["Brand A"],
    "tagged_count": 2,
    "attached_count": 3,
    "desc_phenomenon": "现象",
    "desc_attribution": "归因",
    "desc_suggestion": "建议",
    "first_week": "2026-W01",
    "last_week": "2026-W02",
}

PROBLEM_AXIS = {
    "axis_id": "axis-problem",
    "generation": "OPP2-",
    "axis_type": "通病",
    "axis_name": "锁定机构失效",
    "summary": "多个承力锁定部位出现松脱。",
    "n_cards": 2,
    "n_spu": 3,
    "n_eff": 2.4,
    "evi_total": 12,
    "top_spus": [{"spu": "SPU-1", "evi": 7}, {"spu": "SPU-2", "evi": 5}],
    "run_id": "strategy-test",
    "computed_at": "2026-08-21 10:00",
}

THEME_AXIS = {
    "axis_id": "axis-theme",
    "generation": "OPP2-",
    "axis_type": "诉求",
    "axis_name": "便携支撑快速适配",
    "summary": "用户希望减少携行与转接负担。",
    "n_cards": 2,
    "n_spu": None,
    "n_eff": None,
    "evi_total": 9,
    "top_spus": None,
    "run_id": "strategy-test",
    "computed_at": "2026-08-21 10:00",
}


class PageDatabase:
    def __init__(self):
        self.empty_strategy = False

    def query_one(self, sql, params=None):
        if sql == Q.SHELL_COUNTS:
            return {"products": 1, "iter": 1, "inno": 1, "strategy": 1, "revived": 0}
        if sql == Q.SPU_DETAIL:
            return dict(SPU)
        if sql == Q.ISSUE_DETAIL:
            return dict(ISSUE)
        if sql == Q.INNOVATION_DETAIL:
            return dict(INNO)
        if sql == Q.STRATEGY_AXIS:
            axis_id = params[1]
            if axis_id == PROBLEM_AXIS["axis_id"]:
                return dict(PROBLEM_AXIS)
            if axis_id == THEME_AXIS["axis_id"]:
                return dict(THEME_AXIS)
            return None
        raise AssertionError("unexpected query_one")

    def query(self, sql, params=None):
        if sql == Q.BOARD_SPUS:
            row = dict(SPU)
            row.update({"category_facets": {"__all__": 1, "支架类": 1}})
            return [row]
        if sql in (Q.BOARD_ISSUES, Q.SPU_ISSUES):
            return [dict(ISSUE)]
        if sql == Q.ISSUE_VOICES:
            return [{
                "voice_text": "原声内容",
                "star": 4,
                "platform": "Amazon",
                "country": "US",
                "publish_time": "2026-08-01",
            }, {
                # 外语切片：小字原文可逐字定位（应出 <mark>），并跟中文翻译
                "voice_text": "the tip constantly wobbles",
                "full_content": "I bought it, but the tip constantly wobbles even with light use.",
                "translation": "我买了它，但轻微使用时顶端也一直晃。",
                "star": 2,
                "platform": "Amazon",
                "country": "US",
                "publish_time": "2026-08-02",
            }]
        if sql == Q.SPU_RAW_VOICES:
            return [{
                "message_id": "MSG-RAW", "cls": "诉求缺口",
                "claim": "希望支持更稳的固定方式", "content": "原始内容",
                "platform": "小红书", "publish_time": "2026-08-02",
            }]
        if sql == Q.BOARD_INNOVATIONS:
            return [dict(INNO)]
        if sql == Q.INNOVATION_EVIDENCE:
            return [{
                "message_id": "MSG-INNO",
                "content": "你们动作太慢了，适配 Luna 的产品已经上市了。",
                "content_zh": None,
                "hit_count": 2,
                "hits": [
                    {"seq": 0, "snippet": "你们动作太慢了", "snippet_raw": None},
                    {"seq": 1, "snippet": "适配 Luna 的产品", "snippet_raw": None},
                ],
                "platform": "Reddit",
                "interactions": 18,
                "publish_time": "2026-08-01",
                "url": "https://example.com/post",
            }]
        if sql == Q.STRATEGY_AXES:
            if self.empty_strategy:
                return []
            return [dict(PROBLEM_AXIS if params[-1] == "通病" else THEME_AXIS)]
        if sql == Q.STRATEGY_AXIS_MEMBERS:
            if params[1] == PROBLEM_AXIS["axis_id"]:
                return [{
                    "axis_id": PROBLEM_AXIS["axis_id"],
                    "generation": "OPP2-", "axis_type": "通病",
                    "opp_id": "OPP-1", "role": "leader",
                    "cos_to_leader": 1.0, "link_spu": "SPU-1",
                    "problem_mode": "车载支架锁定后持续松动",
                    "title": "车载支架易松动", "evi_total": 7,
                    "status": "考虑中", "revived_at": None,
                    "product_names": ["测试产品"],
                }]
            return [{
                "axis_id": THEME_AXIS["axis_id"],
                "generation": "OPP2-", "axis_type": "诉求",
                "opp_id": "INNO-1", "role": "leader",
                "cos_to_leader": 1.0, "link_spu": None,
                "problem_mode": "便携支撑需要快速适配不同设备",
                "title": "新场景创新需求", "evi_total": 9,
                "status": "考虑中", "revived_at": None,
                "product_names": [],
            }]
        raise AssertionError("unexpected query")


@pytest.fixture
def page_client(monkeypatch):
    fake = PageDatabase()
    for module in (board, innovation, issue, search, spu, strategy, web):
        monkeypatch.setattr(module, "db", fake)

    app = FastAPI()
    static_dir = Path(board.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    for router in (
        board.router,
        spu.router,
        issue.router,
        innovation.router,
        strategy.router,
        search.router,
        architecture.router,
    ):
        app.include_router(router)
    client = TestClient(app)
    client.app.state.page_db = fake
    return client


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/iter", "老品迭代"),
        ("/spu/SPU-1", "组内中位"),
        ("/issue/SPU-1/OPP-1", "原声内容"),
        ("/inno", "共享池"),
        ("/inno/INNO-1", "建议 · 市面缺口"),
        ("/strategy", "扩散面"),
        ("/architecture", "数据架构"),
    ],
)
def test_all_page_routes_render(page_client, path, expected):
    response = page_client.get(path)
    assert response.status_code == 200
    assert expected in response.text


def test_issue_voice_shows_inline_original_and_translation(page_client):
    """UIUX 验收：切片正句下小字直显原文（命中时高亮切片），非中文跟中文翻译。"""
    html = page_client.get("/issue/SPU-1/OPP-1").text
    assert "原文：" in html and "中文：" in html
    assert "<mark>the tip constantly wobbles</mark>" in html
    assert "我买了它，但轻微使用时顶端也一直晃。" in html
    assert "<details" not in html          # 直显，不折叠


def test_innovation_voice_groups_message_and_marks_all_fragments(page_client):
    html = page_client.get("/inno/INNO-1").text
    assert "1 条原声" in html
    assert "命中 2 个片段" in html
    assert "<mark>你们动作太慢了</mark>" in html
    assert "<mark>适配 Luna 的产品</mark>" in html
    assert html.count('class="voice social-voice"') == 1
    assert "查看原帖" in html


def test_legacy_search_route_is_a_permanent_redirect(page_client):
    response = page_client.get("/search", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/iter?filter=all"


def test_spu_detail_renders_unclaimed_social_voices(page_client):
    response = page_client.get("/spu/SPU-1")
    assert "未入机会点原声" in response.text
    assert "希望支持更稳的固定方式" in response.text
    assert "诉求缺口" in response.text


def test_strategy_two_tabs_axis_detail_404_and_empty_state(page_client):
    overview = page_client.get("/strategy")
    assert overview.status_code == 200
    assert "跨SPU通病" in overview.text and "核心诉求" in overview.text

    theme = page_client.get("/strategy?type=诉求")
    assert theme.status_code == 200 and THEME_AXIS["axis_name"] in theme.text

    detail = page_client.get(f"/strategy/axis/{PROBLEM_AXIS['axis_id']}")
    assert detail.status_code == 200 and PROBLEM_AXIS["summary"] in detail.text
    assert page_client.get("/strategy/axis/missing-axis").status_code == 404

    page_client.app.state.page_db.empty_strategy = True
    try:
        empty = page_client.get("/strategy")
    finally:
        page_client.app.state.page_db.empty_strategy = False
    assert "战略轴尚未计算，等待首次聚合运行。" in empty.text


def test_strategy_member_links_are_followed_and_resolve_non_404(page_client):
    problem = page_client.get(f"/strategy/axis/{PROBLEM_AXIS['axis_id']}")
    problem_links = re.findall(r'data-href="(/issue/[^"]+)"', problem.text)
    assert problem_links
    assert page_client.get(problem_links[0]).status_code != 404

    theme = page_client.get(f"/strategy/axis/{THEME_AXIS['axis_id']}")
    theme_links = re.findall(r'data-href="(/inno/[^"]+)"', theme.text)
    assert theme_links
    assert page_client.get(theme_links[0]).status_code != 404


def test_home_strategy_count_reads_derived_axis_table():
    compact = " ".join(Q.SHELL_COUNTS.split()).lower()
    assert "select count(*)::int from voc_strategy_axis" in compact
    assert "scope_source" not in compact


def test_architecture_page_is_static_and_self_contained(page_client):
    """说明页不查库、不引外部资源；三张图各自内联，口径文字都在页内。"""
    page = page_client.get("/architecture").text.split('<main class="view">')[1]
    for heading in ("总览", "G 系列 · 入池", "老品迭代管道", "新品创新管道", "关键取舍", "术语"):
        assert heading in page
    figures = page.count("<svg viewBox")
    assert figures >= 3
    # marker id 必须逐图唯一：同一文档内 id 冲突会让箭头只在首图渲染。
    markers = re.findall(r'<marker id="([^"]+)"', page)
    assert len(markers) == figures
    assert len(set(markers)) == figures
    # 图内联、无脚本、无外链，页面因此不依赖任何管道产出与外部资源。
    assert "<script" not in page
    assert "http://" not in page and "https://" not in page


def test_architecture_entry_present_and_highlights_only_itself(page_client):
    arch = page_client.get("/architecture").text
    assert 'class="nav nav-lv1 on"' in arch
    other = page_client.get("/iter").text
    assert 'href="/architecture"' in other
    assert 'class="nav nav-lv1"' in other      # 别的页面上它不高亮
    # 二级业务页高亮不外溢到一级分区。
    assert 'class="nav nav-lv2 on"' in other


def test_tag_tree_filter_is_retired() -> None:
    """标签树筛选（体验域/子域/叶子）已退役，只保留品类。

    81 个共有标签是数据处理用的特征，不作为界面筛选条件暴露——
    信息量大且不可行动（2026-08-19 实测：35.2% 的标签体量属于
    「泛质量观感/非产品本体」，推不出工程动作）。
    """
    from pathlib import Path

    for name in ("BOARD_SPUS_BY_DOMAIN", "BOARD_SPUS_BY_SUB", "BOARD_SPUS_BY_LEAF"):
        assert not hasattr(Q, name), f"{name} 应已删除"
    assert "tag_options" not in Q.BOARD_SPUS

    tpl = (Path(__file__).resolve().parents[1]
           / "app" / "templates" / "board.html").read_text(encoding="utf-8")
    for gone in ("体验域", "子域", "叶子", "tag_options", "data-tag-toggle"):
        assert gone not in tpl, f"模板仍残留 {gone}"
    assert 'data-tag-level="category"' in tpl, "品类筛选必须保留"
