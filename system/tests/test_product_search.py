from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.routes import board, search


META = {
    "category_facets": {"__all__": 3, "S支架类": 2, "未分类": 1},
}
TRACKED = {
    "spu": "SPU-TRACKED", "product_names": ["跟踪中产品"], "skus": ["SKU-1"],
    "category": "S支架类", "prod_line": "支撑", "grade": "A级",
    "launch_period": "2025Q4", "has_ec": True, "message_count": 12,
    "negative_evi_count": 3, "positive_evi_count": 7, "negative_ratio": .3,
    "avg_star": 3.8, "issue_count": 1, "open_issue_count": 1,
    "recent_evi_count": 1, "raw_voice_count": 0, "has_revived_issue": False,
}
SOCIAL_ONLY = {
    "spu": "SPU-SOCIAL", "product_names": ["社媒新品"], "skus": [],
    "category": None, "prod_line": None, "grade": None,
    "launch_period": None, "has_ec": False, "message_count": 8,
    "negative_evi_count": None, "positive_evi_count": None,
    "negative_ratio": None, "avg_star": None, "issue_count": 0,
    "open_issue_count": 0, "recent_evi_count": 0, "raw_voice_count": 2,
    "has_revived_issue": False,
}
CLEAN = {
    "spu": "SPU-CLEAN", "product_names": ["干净产品"], "skus": [],
    "category": "S支架类", "prod_line": "支撑", "grade": "B级",
    "launch_period": "2026Q1", "has_ec": True, "message_count": 3,
    "negative_evi_count": 0, "positive_evi_count": 3, "negative_ratio": 0,
    "avg_star": 4.9, "issue_count": 0, "open_issue_count": 0,
    "recent_evi_count": 0, "raw_voice_count": 0, "has_revived_issue": False,
}
ISSUE = {
    "spu": "SPU-TRACKED", "opp_id": "OPP-1", "title": "跟踪问题",
    "evi_count": 3, "status": "考虑中", "revived_at": None,
    "tax_path": None, "recent_evi_count": 1,
}


class MergedBoardDatabase:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []

    def query_one(self, sql, params=None):
        self.calls.append((sql, params))
        assert sql == Q.SHELL_COUNTS
        return {"products": 3, "iter": 1, "inno": 5, "strategy": 2, "revived": 0}

    def query(self, sql, params=None):
        values = tuple(params) if params is not None else None
        self.calls.append((sql, values))
        if sql == Q.BOARD_ISSUES:
            return [dict(ISSUE)]
        if sql == Q.BOARD_SPUS:
            rows = [dict(TRACKED)]
            if values and values[0] == "all":
                rows.extend((dict(SOCIAL_ONLY), dict(CLEAN)))
            rows[0].update(META)
            return rows
        raise AssertionError("unexpected SQL")

    def latest_board_call(self):
        return next(call for call in reversed(self.calls)
                    if call[0] == Q.BOARD_SPUS)


@pytest.fixture
def merged_client(monkeypatch):
    fake = MergedBoardDatabase()
    for module in (board, search, web):
        monkeypatch.setattr(module, "db", fake)
    app = FastAPI()
    static_dir = Path(board.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(board.router)
    app.include_router(search.router)
    return TestClient(app), fake


def test_search_redirects_permanently_and_maps_supported_filters(merged_client):
    client, fake = merged_client
    response = client.get(
        "/search",
        params={"q": "MagLock", "category": "S支架类", "domain": "使用反馈",
                "unknown": "drop-me"},
        follow_redirects=False,
    )
    assert response.status_code == 301
    target = urlsplit(response.headers["location"])
    assert target.path == "/iter"
    assert parse_qs(target.query) == {
        "q": ["MagLock"], "category": ["S支架类"],
        "domain": ["使用反馈"], "filter": ["all"],
    }
    assert fake.calls == []


def test_merged_page_passes_keyword_and_category_to_one_spu_query(merged_client):
    client, fake = merged_client
    response = client.get("/iter", params={"q": "MagLock", "category": "S支架类"})
    _, params = fake.latest_board_call()
    assert response.status_code == 200
    assert params[:6] == (
        "tracked", "MagLock", "%MagLock%", "%MagLock%", "%MagLock%", "S支架类"
    )


def test_default_is_tracked_and_clearing_filter_restores_all_spus(merged_client):
    client, fake = merged_client
    default = client.get("/iter")
    all_spus = client.get("/iter", params={"filter": "all"})
    board_filters = [params[0] for sql, params in fake.calls if sql == Q.BOARD_SPUS]
    assert board_filters == ["tracked", "all"]
    assert "跟踪中产品" in default.text
    assert "社媒新品" not in default.text
    assert "跟踪中产品" in all_spus.text
    assert "社媒新品" in all_spus.text
    assert "干净产品" in all_spus.text
    assert 'class="tab on" href="/iter">有跟踪中问题' in default.text


def test_unknown_status_filter_falls_back_to_tracked(merged_client):
    client, fake = merged_client
    response = client.get("/iter", params={"filter": "legacy-value"})
    assert response.status_code == 200
    assert fake.latest_board_call()[1][0] == "tracked"
    assert "社媒新品" not in response.text


def test_social_only_and_clean_cards_render_explicit_states(merged_client):
    client, _ = merged_client
    html = client.get("/iter", params={"filter": "all"}).text
    social = re.search(r'<tr[^>]*data-spu="SPU-SOCIAL".*?</tr>', html, re.DOTALL)
    clean = re.search(r'<tr[^>]*data-spu="SPU-CLEAN".*?</tr>', html, re.DOTALL)
    assert social and clean
    assert "仅社媒信号" in social.group(0)
    assert social.group(0).count("—") >= 5
    assert "原声 2" in social.group(0)
    assert "无信号" in clean.group(0)


def test_sidebar_has_four_items_and_dual_iteration_count(merged_client):
    client, _ = merged_client
    html = client.get("/iter").text
    sidebar = re.search(r'<aside class="sb">(.*?)<div class="sb-footer">', html, re.DOTALL)
    assert sidebar
    assert sidebar.group(1).count("nav-lv1") == 2
    assert sidebar.group(1).count("nav-lv2") == 3
    assert "产品检索" not in sidebar.group(1)
    assert ">1</span>" in sidebar.group(1)      # 老品迭代读数：跟踪中的产品数
