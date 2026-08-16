from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.routes import search


class SearchDatabase:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []

    def query_one(self, sql, params=None):
        self.calls.append((sql, params))
        assert sql == Q.SHELL_COUNTS
        return {"iter": 51, "inno": 5, "strategy": 68, "search": 328, "revived": 2}

    def query(self, sql, params=None):
        values = tuple(params) if params is not None else None
        self.calls.append((sql, values))
        if sql == Q.SEARCH_FACETS:
            return [
                {"value": "__all__", "count": 1},
                {"value": "S支架类", "count": 1},
            ]
        if sql == Q.SEARCH_TAG_FACETS:
            return []
        if sql == Q.SEARCH_SPUS:
            return [{
                "spu": "SPU-EMPTY", "product_names": ["没有待办的产品"],
                "skus": ["SKU-0"], "category": "S支架类", "prod_line": "支撑",
                "grade": "A级", "launch_period": "2025Q4", "message_count": 12,
                "negative_evi_count": 0, "positive_evi_count": 9,
                "negative_ratio": 0, "avg_star": 4.8,
                "issue_count": 0, "open_issue_count": 0,
                "recent_evi_count": 0,
            }]
        raise AssertionError("unexpected SQL")

    def result_call(self):
        return next(call for call in self.calls if call[0] == Q.SEARCH_SPUS)


@pytest.fixture
def search_client(monkeypatch):
    fake = SearchDatabase()
    monkeypatch.setattr(search, "db", fake)
    monkeypatch.setattr(web, "db", fake)
    app = FastAPI()
    static_dir = Path(search.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(search.router)
    return TestClient(app), fake


def test_search_filters_by_keyword_across_product_fields(search_client):
    client, fake = search_client
    response = client.get("/search", params={"q": "MagLock"})
    sql, params = fake.result_call()
    assert response.status_code == 200
    assert sql == Q.SEARCH_SPUS
    assert params[:4] == ("MagLock", "%MagLock%", "%MagLock%", "%MagLock%")
    assert params[-2:] == ("", "")


def test_search_filters_by_category(search_client):
    client, fake = search_client
    response = client.get("/search", params={"category": "S支架类"})
    _, params = fake.result_call()
    assert response.status_code == 200
    assert params[4:6] == ("S支架类", "S支架类")
    assert "COALESCE(s.category, '未分类')" in Q.SEARCH_SPUS


def test_search_keeps_spu_without_pending_issues(search_client):
    client, fake = search_client
    response = client.get("/search")
    sql, _ = fake.result_call()
    assert response.status_code == 200
    assert "LEFT JOIN" in sql
    assert "没有待办的产品" in response.text
    assert "SPU-EMPTY" in response.text
    assert "0/0" in response.text
    assert 'class="nav on" href="/search"' in response.text
