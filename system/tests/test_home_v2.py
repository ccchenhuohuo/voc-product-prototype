from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.routes import home
from app.viewmodels import HOME2_STATES, normalize_home_dist, normalize_home_v2


SOURCE_ROWS = [
    {"source_order": 1, "source_key": "ec", "source_label": "电商评论", "src_line": "电商", "query_task_type": "COMMENT", "used_count": 26407, "available_count": 87357, "window_start": datetime(2026, 2, 16, tzinfo=timezone.utc), "window_end": datetime(2026, 8, 18, tzinfo=timezone.utc), "probed_at": datetime(2026, 8, 18, tzinfo=timezone.utc)},
    {"source_order": 2, "source_key": "social", "source_label": "社交媒体", "src_line": "社媒", "query_task_type": "SOCIAL", "used_count": 24519, "available_count": 178389, "window_start": datetime(2026, 2, 16, tzinfo=timezone.utc), "window_end": datetime(2026, 8, 18, tzinfo=timezone.utc), "probed_at": datetime(2026, 8, 18, tzinfo=timezone.utc)},
    {"source_order": 3, "source_key": "service", "source_label": "客服会话", "src_line": "客服", "query_task_type": "SERVICE", "used_count": 0, "available_count": 1250847, "window_start": datetime(2026, 2, 16, tzinfo=timezone.utc), "window_end": datetime(2026, 8, 18, tzinfo=timezone.utc), "probed_at": datetime(2026, 8, 18, tzinfo=timezone.utc)},
]

SOCIAL_ROWS = [
    (1, "产品种草广告", 13556, 64, 237),
    (2, "用户咨询", 4936, 88, 762),
    (3, "用户使用体验", 3260, 96, 853),
    (4, "产品评测", 2515, 113, 524),
    (5, "竞品拉踩", 111, 0, 6),
    (6, "其他", 63, 2, 4),
    (7, "无标签", 76, 0, 0),
    (8, "二手转让", 2, 0, 0),
]
SOCIAL_ROWS = [
    {"label_order": order, "label": label, "message_count": total,
     "iter_count": iteration, "inno_count": innovation,
     "source_total": 24519, "label_class_count": 7}
    for order, label, total, iteration, innovation in SOCIAL_ROWS
]

EC_ROWS = [
    (1, "产品材质", 2166, 301),
    (2, "重量", 1549, 238),
    (3, "整体质量", 1497, 172),
    (4, "尺寸大小", 1072, 234),
    (5, "推荐意愿", 1153, 92),
    (6, "新手友好", 1096, 72),
    (7, "其余 210 类", 13813, 2330),
    (8, "无标签", 4061, 0),
]
EC_ROWS = [
    {"bucket_order": order, "bucket_key": "top", "label": label,
     "message_count": total, "iter_count": iteration, "inno_count": 0,
     "source_total": 26407, "label_class_count": 229,
     "remaining_class_count": 210}
    for order, label, total, iteration in EC_ROWS
]


def status_rows() -> list[dict]:
    rows = []
    for lifecycle_order, lifecycle, total, anchor in (
        (1, "老品迭代", 916, "锚定 SPU × 问题"),
        (2, "新品创新", 2991, "锚定机会点"),
    ):
        for status_order, status in enumerate(HOME2_STATES, 1):
            rows.append({
                "row_type": "lifecycle", "lifecycle_order": lifecycle_order,
                "lifecycle": lifecycle, "anchor": anchor,
                "status_order": status_order, "status": status,
                "item_count": total if status == "考虑中" else 0,
                "machine_order": None, "machine_label": None,
            })
    for order, label, value in (
        (1, "已放行", 331), (2, "待复核", 406),
        (3, "安全通道", 65), (4, "未放行", 3535),
    ):
        rows.append({
            "row_type": "machine", "lifecycle_order": None,
            "lifecycle": None, "anchor": None, "status_order": None,
            "status": None, "item_count": value,
            "machine_order": order, "machine_label": label,
        })
    return rows


FRESHNESS_ROWS = [{
    "latest_publish_time": datetime(2026, 8, 14, 23, 21, tzinfo=timezone.utc),
    "coverage_weeks": 26, "spu_count": 329,
    "spu_issue_total": 916, "spu_with_issue_count": 140,
    "dangling_count": 0, "dangling_total": 4810,
    "issue_dangling": 0, "nn_dangling": 0, "dangling_pct": 0,
    "generation_id": "gen_fixture", "generation_week": "2026-W33",
    "ledger_complete": True, "ledger_item_count": 56,
}]

WEEKLY_ROWS = [
    {"week_order": index + 1, "week_label": f"2026-W{index + 8:02d}",
     "week_start": datetime(2026, 2, 16, tzinfo=timezone.utc) + timedelta(weeks=index),
     "social_count": 500 + index, "ec_count": 900 + index}
    for index in range(26)
]

DIST_ROWS = {
    ("社媒", "语种"): [
        {"src": "社媒", "dim": "语种", "item_order": 1, "label": "中文", "item_count": 12, "distribution_total": 20},
        {"src": "社媒", "dim": "语种", "item_order": 2, "label": "英文", "item_count": 8, "distribution_total": 20},
    ],
    ("社媒", "平台"): [
        {"src": "社媒", "dim": "平台", "item_order": 1, "label": "小红书", "item_count": 20, "distribution_total": 20},
    ],
    ("电商", "语种"): [
        {"src": "电商", "dim": "语种", "item_order": 1, "label": "未标注", "item_count": 26407, "distribution_total": 26407},
    ],
    ("电商", "平台"): [
        {"src": "电商", "dim": "平台", "item_order": 1, "label": "天猫", "item_count": 13, "distribution_total": 20},
        {"src": "电商", "dim": "平台", "item_order": 2, "label": "京东", "item_count": 7, "distribution_total": 20},
    ],
    ("客服", "语种"): [],
    ("客服", "平台"): [],
}

FULL_ROWS = {
    Q.HOME2_SOURCES: SOURCE_ROWS,
    Q.HOME2_FLOW_SOCIAL: SOCIAL_ROWS,
    Q.HOME2_FLOW_EC: EC_ROWS,
    Q.HOME2_STATUS: status_rows(),
    Q.HOME2_FRESHNESS: FRESHNESS_ROWS,
    Q.HOME2_WEEKLY: WEEKLY_ROWS,
}


def dashboard():
    return normalize_home_v2(
        source_rows=deepcopy(SOURCE_ROWS), social_rows=deepcopy(SOCIAL_ROWS),
        ec_rows=deepcopy(EC_ROWS), status_rows=status_rows(),
        freshness_rows=deepcopy(FRESHNESS_ROWS), weekly_rows=deepcopy(WEEKLY_ROWS),
        dist_rows=deepcopy(DIST_ROWS[("社媒", "语种")]),
    )


class HomeV2Database:
    def __init__(self):
        self.calls = []

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        if sql == Q.HOME2_DIST:
            return deepcopy(DIST_ROWS[tuple(params)])
        if params is not None:
            raise AssertionError("unexpected params")
        return deepcopy(FULL_ROWS[sql])

    def query_one(self, sql, params=None):
        assert sql == Q.SHELL_COUNTS
        return {"products": 0, "iter": 0, "inno": 0, "strategy": 0, "revived": 0}


@pytest.fixture
def client(monkeypatch):
    fake = HomeV2Database()
    monkeypatch.setattr(home, "db", fake)
    monkeypatch.setattr(web, "db", fake)
    app = FastAPI()
    static_dir = Path(home.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(home.router)
    return TestClient(app), fake


def test_unique_attribution_conserves_each_source_total():
    result = dashboard()
    assert sum(row["total"] for row in result["flow"]["social_rows"]) == 24519
    assert sum(row["total"] for row in result["flow"]["ec_rows"]) == 26407


def test_entered_total_and_lifecycle_totals_are_conserved():
    result = dashboard()
    old_total = sum(row["iter"] for row in result["flow"]["social_rows"] + result["flow"]["ec_rows"])
    new_total = sum(row["inno"] for row in result["flow"]["social_rows"] + result["flow"]["ec_rows"])
    assert old_total == 3802
    assert new_total == 2386
    assert result["flow"]["entered"] == 6188 == old_total + new_total
    assert sum(tag["entered"] for tag in result["flow"]["sankey"]["tags"]) == 6188


def test_ecommerce_never_flows_to_innovation():
    result = dashboard()
    assert sum(row["inno"] for row in result["flow"]["ec_rows"]) == 0


def test_missing_manual_rows_all_land_in_considering():
    result = dashboard()
    for lifecycle in result["status"]["lifecycles"]:
        counts = {row["state"]: row["value"] for row in lifecycle["status_keys"]}
        assert counts["考虑中"] == lifecycle["total"]
        assert sum(counts[state] for state in HOME2_STATES[1:]) == 0
    assert result["status"]["cold_start"] is True


def test_sankey_geometry_stays_in_viewbox_and_conserves_band_height():
    sankey = dashboard()["flow"]["sankey"]
    for node in sankey["sources"] + sankey["tags"] + sankey["destinations"]:
        assert 0 <= node["x"] <= sankey["width"]
        assert node["x"] + node["width"] <= sankey["width"]
        assert 0 <= node["y"]
        assert node["y"] + node["height"] <= sankey["height"]

    label_positions = [node["label_y"] for node in sankey["tags"]]
    assert all(
        later - earlier >= sankey["label_gap"]
        for earlier, later in zip(label_positions, label_positions[1:])
    )
    assert any(node["leader_d"] for node in sankey["tags"])

    source_bands = sum(
        band["height"] for band in sankey["bands"]
        if band["stage"] == "source_tag"
    )
    destination_bands = sum(
        band["height"] for band in sankey["bands"]
        if band["stage"] == "tag_dest"
    )
    assert source_bands == pytest.approx(sankey["column_height"])
    assert destination_bands == pytest.approx(sankey["column_height"])
    assert sankey["source_height_sum"] == pytest.approx(sankey["column_height"])
    assert sankey["tag_height_sum"] == pytest.approx(sankey["column_height"])
    assert sankey["destination_height_sum"] == pytest.approx(sankey["column_height"])


def test_ecommerce_language_is_an_explained_empty_state():
    result = normalize_home_dist(DIST_ROWS[("电商", "语种")], src="电商", dim="语种")
    assert result["empty"] is True
    assert "全部为「未标注」" in result["empty_message"]
    assert result["arcs"] == []


@pytest.mark.parametrize("dim", ("语种", "平台"))
def test_service_distribution_is_an_explained_empty_state(dim):
    result = normalize_home_dist([], src="客服", dim=dim)
    assert result["empty"] is True
    assert result["empty_message"] == "客服会话尚未接入，无分布数据"


def test_home_route_renders_four_modules_and_queries_all_sources(client):
    browser, fake = client
    response = browser.get("/")
    assert response.status_code == 200
    for text in ("源数据三管道", "数据流转", "状态分布", "数据新鲜度"):
        assert text in response.text
    assert "管道运行" not in response.text
    assert fake.calls == [
        (Q.HOME2_SOURCES, None), (Q.HOME2_FLOW_SOCIAL, None),
        (Q.HOME2_FLOW_EC, None), (Q.HOME2_STATUS, None),
        (Q.HOME2_FRESHNESS, None), (Q.HOME2_WEEKLY, None),
        (Q.HOME2_DIST, ("社媒", "语种")),
    ]


@pytest.mark.parametrize(
    "src,dim", [(src, dim) for src in ("社媒", "电商", "客服") for dim in ("语种", "平台")],
)
def test_distribution_route_all_valid_combinations_return_200(client, src, dim):
    browser, _ = client
    response = browser.get("/home/dist", params={"src": src, "dim": dim})
    assert response.status_code == 200


def test_distribution_route_invalid_parameters_fall_back_without_500(client):
    browser, fake = client
    response = browser.get("/home/dist", params={"src": "未知", "dim": "未知"})
    assert response.status_code == 200
    assert "中文" in response.text
    assert fake.calls[-1] == (Q.HOME2_DIST, ("社媒", "语种"))


def test_probe_cache_missing_is_not_rendered_as_zero():
    rows = deepcopy(SOURCE_ROWS)
    rows[0]["available_count"] = None
    result = normalize_home_v2(
        source_rows=rows, social_rows=SOCIAL_ROWS, ec_rows=EC_ROWS,
        status_rows=status_rows(), freshness_rows=FRESHNESS_ROWS,
        weekly_rows=WEEKLY_ROWS, dist_rows=DIST_ROWS[("社媒", "语种")],
    )
    assert result["sources"]["cards"][0]["available_label"] == "未探测"
    assert result["sources"]["cards"][0]["rate"] is None


def test_business_snapshot_numbers_are_not_embedded_in_runtime_frontend_code():
    root = Path(__file__).resolve().parents[1]
    files = [
        root / "app/viewmodels.py", root / "app/routes/home.py",
        root / "app/templates/home.html", root / "app/templates/_home_dist.html",
        root / "app/static/app.js",
    ]
    runtime = "\n".join(path.read_text() for path in files)
    for snapshot in (
        "26407", "24519", "6188", "3802", "2386", "2991", "87.8", "12.2",
    ):
        assert snapshot not in runtime
