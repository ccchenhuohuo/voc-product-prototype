from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.routes import board


SPUS = [
    {
        "spu": "SPU-HOT", "product_names": ["高占比产品"], "skus": [],
        "category": "支架类", "prod_line": "支撑", "grade": "A级",
        "launch_period": "2026Q1", "has_ec": True, "negative_evi_count": 8,
        "positive_evi_count": 2, "negative_ratio": 0.8, "avg_star": 3.7,
        "open_issue_count": 1, "issue_count": 1, "recent_evi_count": 2,
        "raw_voice_count": 0, "max_negative_evi_count": 8,
        "has_revived_issue": False,
    },
    {
        "spu": "SPU-REV", "product_names": ["曾复活产品"], "skus": [],
        "category": "支架类", "prod_line": "支撑", "grade": "PS级",
        "launch_period": "2026Q1", "has_ec": True, "negative_evi_count": 1,
        "positive_evi_count": 9, "negative_ratio": 0.1, "avg_star": 4.6,
        "open_issue_count": 1, "issue_count": 1, "recent_evi_count": 1,
        "raw_voice_count": 0, "max_negative_evi_count": 8,
        "has_revived_issue": True,
    },
]
ISSUES = [
    {
        "spu": "SPU-HOT", "opp_id": "OPP-HOT", "title": "普通问题",
        "evi_count": 8, "status": "考虑中", "revived_at": None,
        "tax_path": None, "recent_evi_count": 2,
    },
    {
        "spu": "SPU-REV", "opp_id": "OPP-REV", "title": "复活问题",
        "evi_count": 1, "status": "考虑中",
        "revived_at": datetime.now(timezone.utc), "tax_path": None,
        "recent_evi_count": 1,
    },
]
class ControlsDatabase:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []
        self.revived_spus = [dict(SPUS[1])]

    def query_one(self, sql, params=None):
        self.calls.append((sql, tuple(params) if params is not None else None))
        assert sql == Q.SHELL_COUNTS
        return {"products": 2, "iter": 2, "inno": 0, "strategy": 0, "revived": 1}

    def query(self, sql, params=None):
        values = tuple(params) if params is not None else None
        self.calls.append((sql, values))
        if sql == Q.BOARD_ISSUES:
            return [dict(row) for row in ISSUES]
        if sql == Q.BOARD_SPUS:
            rows = self.revived_spus if values and values[0] == "revived" else SPUS
            result = [dict(row) for row in rows]
            meta = {"category_facets": {"__all__": 2, "支架类": 2}}
            if result:
                result[0].update(meta)
            else:
                result = [{"spu": None, **meta}]
            return result
        raise AssertionError("unexpected query")

    def latest(self, choices: tuple[str, ...]) -> tuple[str, tuple | None]:
        return next(call for call in reversed(self.calls) if call[0] in choices)


@pytest.fixture
def controls_client(monkeypatch):
    fake = ControlsDatabase()
    for module in (board, web):
        monkeypatch.setattr(module, "db", fake)
    app = FastAPI()
    static_dir = Path(board.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(board.router)
    return TestClient(app), fake


@pytest.mark.parametrize("key", ["grade", "ratio", "evidence", "star", "issues"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_sortable_columns_support_server_side_sort(controls_client, key, direction):
    client, fake = controls_client
    response = client.get("/iter", params={"sort": key, "dir": direction})
    assert response.status_code == 200
    _, params = fake.latest((Q.BOARD_SPUS,))
    assert params[-2:] == (key, direction)
    assert 'class="sort-col' in response.text and ' sorted"' in response.text


def test_sort_header_cycles_asc_desc_then_default(controls_client):
    client, _ = controls_client
    default = client.get("/iter")
    ascending = client.get("/iter", params={"sort": "ratio", "dir": "asc"})
    descending = client.get("/iter", params={"sort": "ratio", "dir": "desc"})
    assert "sort=ratio&amp;dir=asc" in default.text
    assert "sort=ratio&amp;dir=desc" in ascending.text
    active = re.search(r'<th class="sort-col r sorted".*?</th>', descending.text, re.DOTALL)
    assert active and 'href="/iter"' in active.group(0)
    assert 'class="sort-icon idle"' in default.text


def test_status_filters_have_three_real_links_and_default_semantics(controls_client):
    client, _ = controls_client
    html = client.get("/iter").text
    tabs = re.search(r'<nav class="tabs" aria-label="状态筛选">(.*?)</nav>', html, re.DOTALL)
    assert tabs
    assert re.search(r'<a class="tab on" href="/iter">有跟踪中问题', tabs.group(1))
    assert 'href="/iter?filter=revived">曾复活' in tabs.group(1)
    assert 'href="/iter?filter=all">全部' in tabs.group(1)
    assert "我负责的" not in tabs.group(1)


def test_revived_filter_and_sort_parameters_preserve_each_other(controls_client):
    client, fake = controls_client
    response = client.get("/iter?filter=revived&sort=ratio&dir=asc")
    assert response.status_code == 200
    assert fake.latest((Q.BOARD_SPUS,))[1][0] == "revived"
    assert fake.latest((Q.BOARD_SPUS,))[1][-2:] == ("ratio", "asc")
    assert 'href="/iter?filter=revived&amp;sort=ratio&amp;dir=desc"' in response.text
    assert 'data-spu="SPU-REV"' in response.text
    assert 'data-spu="SPU-HOT"' not in response.text


def test_empty_revived_filter_keeps_meaningful_empty_state(controls_client):
    client, fake = controls_client
    fake.revived_spus = []
    response = client.get("/iter?filter=revived")
    assert response.status_code == 200
    assert "当前没有曾复活的产品" in response.text


def test_sort_headers_only_make_arrow_clickable(controls_client):
    client, _ = controls_client
    response = client.get("/iter")
    headers = re.findall(r'<th class="sort-col[^"]*".*?</th>', response.text, re.DOTALL)
    assert len(headers) == 5
    for header in headers:
        label = re.search(r'<span class="sort-label">([^<]+)</span>', header)
        control = re.search(r'<a class="sort-control"[^>]*>(.*?)</a>', header, re.DOTALL)
        assert label and control and label.end() <= control.start()
        assert label.group(1) not in control.group(1)
        assert "<svg " in control.group(1)
    assert 'aria-label="按负面证据升序排列"' in response.text


def test_category_is_the_only_filter_row(controls_client):
    """标签树退役后，筛选模块只剩品类一行。"""
    client, _ = controls_client
    tree = re.search(r'<section class="tag-tree".*?</section>',
                     client.get("/iter").text, re.DOTALL)
    assert tree and "品类" in tree.group(0)
    assert tree.group(0).count("tag-level-title") == 1


def test_board_always_uses_single_spu_query(controls_client):
    """筛选不再分叉出第二次 SPU 查询。"""
    client, fake = controls_client
    client.get("/iter", params={"q": "支架", "category": "支架类"})
    spu_calls = [call for call in fake.calls if call[0] == Q.BOARD_SPUS]
    assert len(spu_calls) == 1
    assert spu_calls[0][1][1:6] == ("支架", "%支架%", "%支架%", "%支架%", "支架类")
    assert "e.tax_domain = %s" not in Q.BOARD_SPUS


def test_only_revived_rows_keep_flag_highlight(controls_client):
    client, _ = controls_client
    html = client.get("/iter").text
    hot = re.search(r'<tr class="([^"]*)"[^>]*data-spu="SPU-HOT"', html)
    revived = re.search(r'<tr class="([^"]*)"[^>]*data-spu="SPU-REV"', html)
    assert hot and "flag" not in hot.group(1)
    assert revived and "flag" in revived.group(1)


def test_sort_control_css_never_underlines_icon_link():
    css = (Path(board.__file__).resolve().parents[1] / "static/app.css").read_text()
    assert ".sort-link" not in css
    assert re.search(r"\.sort-control\s*\{[^}]*text-decoration:\s*none;", css, re.DOTALL)
