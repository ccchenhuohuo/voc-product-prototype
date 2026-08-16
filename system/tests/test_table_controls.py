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
from app.routes import board, search


SPUS = [
    {
        "spu": "SPU-HOT", "product_names": ["高占比产品"], "skus": [],
        "category": "支架类", "prod_line": "支撑", "grade": "A级",
        "launch_period": "2026Q1", "negative_evi_count": 8,
        "positive_evi_count": 2, "negative_ratio": 0.8, "avg_star": 3.7,
        "open_issue_count": 1, "issue_count": 1, "recent_evi_count": 2,
        "max_negative_evi_count": 8, "has_revived_issue": False,
    },
    {
        "spu": "SPU-REV", "product_names": ["待复议产品"], "skus": [],
        "category": "支架类", "prod_line": "支撑", "grade": "PS级",
        "launch_period": "2026Q1", "negative_evi_count": 1,
        "positive_evi_count": 9, "negative_ratio": 0.1, "avg_star": 4.6,
        "open_issue_count": 1, "issue_count": 1, "recent_evi_count": 1,
        "max_negative_evi_count": 8, "has_revived_issue": True,
    },
]

ISSUES = [
    {
        "spu": "SPU-HOT", "opp_id": "OPP-HOT", "title": "普通问题",
        "evi_count": 8, "status": "考虑中", "revived_at": None,
        "tax_path": None, "recent_evi_count": 2,
    },
    {
        "spu": "SPU-REV", "opp_id": "OPP-REV", "title": "复议问题",
        "evi_count": 1, "status": "考虑中",
        "revived_at": datetime.now(timezone.utc), "tax_path": None,
        "recent_evi_count": 1,
    },
]

TAG_ROWS = [
    {"level": "domain", "domain": "产品体验", "sub": None, "leaf": None, "count": 2},
    {"level": "domain", "domain": "购买体验", "sub": None, "leaf": None, "count": 1},
    {"level": "sub", "domain": "产品体验", "sub": "外观尺寸", "leaf": None, "count": 2},
    {"level": "sub", "domain": "产品体验", "sub": "配件", "leaf": None, "count": 1},
    {"level": "sub", "domain": "购买体验", "sub": "物流", "leaf": None, "count": 1},
    {"level": "leaf", "domain": "产品体验", "sub": "外观尺寸", "leaf": "尺寸", "count": 2},
    {"level": "leaf", "domain": "产品体验", "sub": "外观尺寸", "leaf": "重量", "count": 1},
    {"level": "leaf", "domain": "产品体验", "sub": "配件", "leaf": "手机夹", "count": 1},
    {"level": "leaf", "domain": "购买体验", "sub": "物流", "leaf": "时效", "count": 1},
]


class ControlsDatabase:
    def __init__(self):
        self.calls: list[tuple[str, tuple | None]] = []
        self.tag_rows = [dict(row) for row in TAG_ROWS]
        self.revived_spus = [dict(SPUS[1])]

    def query_one(self, sql, params=None):
        self.calls.append((sql, tuple(params) if params is not None else None))
        assert sql == Q.SHELL_COUNTS
        return {"iter": 2, "inno": 0, "strategy": 0, "search": 2, "revived": 1}

    def query(self, sql, params=None):
        values = tuple(params) if params is not None else None
        self.calls.append((sql, values))
        if sql == Q.BOARD_SPUS:
            return [dict(row) for row in SPUS]
        if sql == Q.BOARD_SPUS_REVIVED:
            return [dict(row) for row in self.revived_spus]
        if sql == Q.BOARD_ISSUES:
            return [dict(row) for row in ISSUES]
        if sql == Q.SEARCH_FACETS:
            return [
                {"value": "__all__", "count": 2},
                {"value": "支架类", "count": 2},
            ]
        if sql == Q.SEARCH_TAG_FACETS:
            return [dict(row) for row in self.tag_rows]
        if sql in (
            Q.SEARCH_SPUS,
            Q.SEARCH_SPUS_BY_DOMAIN,
            Q.SEARCH_SPUS_BY_SUB,
            Q.SEARCH_SPUS_BY_LEAF,
        ):
            return [dict(row) for row in SPUS]
        raise AssertionError("unexpected query")

    def latest(self, choices: tuple[str, ...]) -> tuple[str, tuple | None]:
        return next(call for call in reversed(self.calls) if call[0] in choices)


@pytest.fixture
def controls_client(monkeypatch):
    fake = ControlsDatabase()
    for module in (board, search, web):
        monkeypatch.setattr(module, "db", fake)
    app = FastAPI()
    static_dir = Path(board.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(board.router)
    app.include_router(search.router)
    return TestClient(app), fake


@pytest.mark.parametrize("key", ["grade", "ratio", "evidence", "star", "issues"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
@pytest.mark.parametrize("path, query", [("/iter", Q.BOARD_SPUS), ("/search", Q.SEARCH_SPUS)])
def test_sortable_columns_support_server_side_ascending_and_descending_sort(
    controls_client, path, query, key, direction
):
    client, fake = controls_client
    response = client.get(path, params={"sort": key, "dir": direction})
    assert response.status_code == 200
    _, params = fake.latest((query,))
    assert params[-2:] == (key, direction)
    assert 'class="sort-col sorted"' in response.text or 'sorted"' in response.text
    assert f"sort={key}" in response.text or direction == "desc"


def test_sort_header_cycles_asc_desc_then_default(controls_client):
    client, _ = controls_client
    default = client.get("/search")
    ascending = client.get("/search", params={"sort": "ratio", "dir": "asc"})
    descending = client.get("/search", params={"sort": "ratio", "dir": "desc"})
    assert "sort=ratio&amp;dir=asc" in default.text
    assert "sort=ratio&amp;dir=desc" in ascending.text
    active_header = re.search(
        r'<th class="sort-col r sorted".*?</th>', descending.text, re.DOTALL
    )
    assert active_header
    assert 'href="/search"' in active_header.group(0)
    assert 'class="sort-icon idle"' in default.text
    assert "stroke-width: 1.5px" in (
        Path(board.__file__).resolve().parents[1] / "static/app.css"
    ).read_text()


def test_iteration_tabs_have_real_links_and_an_honest_disabled_placeholder(
    controls_client,
):
    client, _ = controls_client
    response = client.get("/iter")
    tabs = re.search(
        r'<nav class="tabs" aria-label="队列视图">(.*?)</nav>',
        response.text,
        re.DOTALL,
    )
    assert tabs
    html = tabs.group(1)
    assert re.search(r'<a class="tab on" href="/iter">全部 ', html)
    assert re.search(r'<a class="tab" href="/iter\?filter=revived">待复议 ', html)
    assert re.search(
        r'<button class="tab" type="button" disabled '
        r'title="待接入认证后可用">我负责的 ',
        html,
    )


def test_revived_filter_and_sort_parameters_preserve_each_other(controls_client):
    client, fake = controls_client
    response = client.get(
        "/iter?filter=revived&sort=ratio&dir=asc"
    )
    assert response.status_code == 200
    assert fake.latest((Q.BOARD_SPUS_REVIVED,))[1] == ("ratio", "asc")
    assert 'class="tab on" href="/iter?filter=revived&amp;sort=ratio&amp;dir=asc"' in response.text
    assert 'href="/iter?sort=ratio&amp;dir=asc">全部' in response.text
    assert 'href="/iter?filter=revived&amp;sort=ratio&amp;dir=desc"' in response.text
    assert 'data-spu="SPU-REV"' in response.text
    assert 'data-spu="SPU-HOT"' not in response.text


def test_empty_revived_filter_keeps_the_meaningful_empty_state(controls_client):
    client, fake = controls_client
    fake.revived_spus = []
    response = client.get("/iter?filter=revived")
    assert response.status_code == 200
    assert "当前没有待复议的条目" in response.text
    assert 'href="/iter?filter=revived"' in response.text


@pytest.mark.parametrize("path", ["/iter", "/search"])
def test_sort_headers_only_make_the_arrow_clickable(controls_client, path):
    client, _ = controls_client
    response = client.get(path)
    headers = re.findall(
        r'<th class="sort-col[^"]*".*?</th>', response.text, re.DOTALL
    )
    assert len(headers) == 5
    for header in headers:
        label = re.search(
            r'<span class="sort-label">([^<]+)</span>', header
        )
        control = re.search(
            r'<a class="sort-control"[^>]*>(.*?)</a>', header, re.DOTALL
        )
        assert label and control
        assert label.end() <= control.start()
        assert label.group(1) not in control.group(1)
        assert "<svg " in control.group(1)
        assert "<button" not in header
    assert "text-decoration: underline" not in response.text
    assert 'aria-label="按负面证据升序排列"' in response.text
    assert 'title="按置信下界排序，样本量少的会被压低"' in response.text


def test_sort_control_css_never_underlines_the_icon_link():
    css = (
        Path(board.__file__).resolve().parents[1] / "static/app.css"
    ).read_text()
    assert ".sort-link" not in css
    assert re.search(
        r"\.sort-control\s*\{[^}]*text-decoration:\s*none;", css, re.DOTALL
    )
    assert re.search(
        r"\.sort-control:hover,\s*\.sort-control:focus-visible\s*"
        r"\{[^}]*text-decoration:\s*none;",
        css,
        re.DOTALL,
    )


def test_grade_sort_uses_business_order_instead_of_alphabetical_order():
    text = " ".join(Q.BOARD_SPUS.split())
    assert "regexp_replace" in text
    positions = [text.index(f"WHEN '{grade}' THEN {rank}") for rank, grade in enumerate(
        ("PS", "S", "A", "B", "C", "D"), start=1
    )]
    assert positions == sorted(positions)


@pytest.mark.parametrize(
    "params, expected_query, tag_params",
    [
        ({}, Q.SEARCH_SPUS, ()),
        ({"domain": "产品体验"}, Q.SEARCH_SPUS_BY_DOMAIN, ("产品体验",)),
        (
            {"domain": "产品体验", "sub": "外观尺寸"},
            Q.SEARCH_SPUS_BY_SUB,
            ("产品体验", "外观尺寸"),
        ),
        (
            {"domain": "产品体验", "sub": "外观尺寸", "leaf": "尺寸"},
            Q.SEARCH_SPUS_BY_LEAF,
            ("产品体验", "外观尺寸", "尺寸"),
        ),
    ],
)
def test_tag_tree_progressively_selects_domain_sub_and_leaf(
    controls_client, params, expected_query, tag_params
):
    client, fake = controls_client
    response = client.get("/search", params=params)
    assert response.status_code == 200
    _, query_params = fake.latest((
        Q.SEARCH_SPUS,
        Q.SEARCH_SPUS_BY_DOMAIN,
        Q.SEARCH_SPUS_BY_SUB,
        Q.SEARCH_SPUS_BY_LEAF,
    ))
    assert fake.latest((expected_query,))[0] == expected_query
    assert query_params[6:6 + len(tag_params)] == tag_params
    if params.get("domain"):
        assert "物流" not in response.text
    if params.get("sub"):
        assert "手机夹" not in response.text
    if params.get("leaf"):
        assert "产品体验 › 外观尺寸 › 尺寸" in response.text
        assert "标签筛选 ›" not in response.text
        assert "清除" in response.text


def test_all_three_tag_levels_are_visible_before_selecting_a_parent(controls_client):
    client, _ = controls_client
    response = client.get("/search")
    assert response.text.count('data-level="domain"') == 2
    assert response.text.count('data-level="sub"') == 3
    assert response.text.count('data-level="leaf"') == 4
    assert "手机夹" in response.text


def test_collapsed_tag_level_keeps_selected_option_in_dom(controls_client):
    client, fake = controls_client
    fake.tag_rows = [
        {"level": "domain", "domain": f"体验域{index}", "sub": None,
         "leaf": None, "count": 20 - index}
        for index in range(1, 11)
    ]
    response = client.get("/search", params={"domain": "体验域9"})
    assert response.status_code == 200
    selected = re.search(
        r'<a class="([^"]*)" data-level="domain"[^>]*>体验域9', response.text
    )
    assert selected
    assert "tag-overflow" in selected.group(1)
    assert "on" in selected.group(1).split()
    assert "展开全部 (10)" in response.text


def test_category_is_first_row_inside_tag_filter_module(controls_client):
    client, _ = controls_client
    response = client.get("/search")
    tree = re.search(r'<section class="tag-tree".*?</section>', response.text, re.DOTALL)
    assert tree
    assert tree.group(0).index("品类") < tree.group(0).index("体验域")
    assert 'aria-label="品类筛选"' not in response.text


def test_no_tag_selection_uses_query_without_evidence_condition(controls_client):
    client, fake = controls_client
    client.get("/search", params={"q": "支架", "category": "支架类"})
    result_sql, _ = fake.latest((Q.SEARCH_SPUS,))
    assert "voc_evidence" not in result_sql
    assert "EXISTS" in Q.SEARCH_SPUS_BY_DOMAIN
    facet_call = fake.latest((Q.SEARCH_TAG_FACETS,))
    assert facet_call[1] == ("支架", "%支架%", "%支架%", "%支架%", "支架类", "支架类")


@pytest.mark.parametrize("path", ["/iter", "/search"])
def test_only_revived_rows_keep_flag_highlight(controls_client, path):
    client, _ = controls_client
    response = client.get(path)
    hot = re.search(r'<tr class="([^"]*)"[^>]*data-spu="SPU-HOT"', response.text)
    revived = re.search(r'<tr class="([^"]*)"[^>]*data-spu="SPU-REV"', response.text)
    assert hot and "flag" not in hot.group(1)
    assert revived and "flag" in revived.group(1)
    assert "alarm-text" in response.text
