from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.routes import board, innovation, issue, search, spu, strategy


SPU = {
    "spu": "SPU-1",
    "product_names": ["测试产品"],
    "skus": ["SKU-1"],
    "category": "支架类",
    "prod_line": "支撑",
    "grade": "A级",
    "launch_period": "2026Q1",
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


class PageDatabase:
    def query_one(self, sql, params=None):
        if sql == Q.SHELL_COUNTS:
            return {"iter": 1, "inno": 1, "strategy": 1, "search": 1, "revived": 0}
        if sql == Q.SPU_DETAIL:
            return dict(SPU)
        if sql == Q.ISSUE_DETAIL:
            return dict(ISSUE)
        if sql == Q.INNOVATION_DETAIL:
            return dict(INNO)
        raise AssertionError("unexpected query_one")

    def query(self, sql, params=None):
        if sql in (Q.BOARD_SPUS, Q.SEARCH_SPUS):
            return [dict(SPU)]
        if sql in (Q.BOARD_ISSUES, Q.SPU_ISSUES):
            return [dict(ISSUE)]
        if sql == Q.ISSUE_VOICES:
            return [{
                "voice_text": "原声内容",
                "star": 4,
                "platform": "Amazon",
                "country": "US",
                "publish_time": "2026-08-01",
            }]
        if sql == Q.BOARD_INNOVATIONS:
            return [dict(INNO)]
        if sql == Q.INNOVATION_EVIDENCE:
            return [{
                "voice_text": "灵感原声",
                "platform": "Reddit",
                "interactions": 18,
                "publish_time": "2026-08-01",
            }]
        if sql == Q.STRATEGY_OPPORTUNITIES:
            return [{
                "opp_id": "OPP-1",
                "title": "支架稳定性",
                "prod_line": "支撑",
                "scope": "品线级",
                "spu_count": 7,
                "n_eff": 4.2,
                "evi_total": 20,
            }]
        if sql == Q.SEARCH_FACETS:
            return [{"value": "__all__", "count": 1}, {"value": "支架类", "count": 1}]
        if sql == Q.SEARCH_TAG_FACETS:
            return []
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
    ):
        app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/iter", "老品迭代"),
        ("/spu/SPU-1", "组内中位"),
        ("/issue/SPU-1/OPP-1", "原声内容"),
        ("/inno", "共享池"),
        ("/inno/INNO-1", "建议 · 市面缺口"),
        ("/strategy", "涉及 SPU"),
        ("/search", "产品检索"),
    ],
)
def test_all_page_routes_render(page_client, path, expected):
    response = page_client.get(path)
    assert response.status_code == 200
    assert expected in response.text
