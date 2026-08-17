from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.routes import home


FUNNEL_ROWS = [
    {"row_type": "stage", "stage_order": 1, "stage_key": "fact_total", "label": "事实层证据总数", "item_count": 1000, "previous_count": None, "retention_pct": None, "loss_tag": None},
    {"row_type": "stage", "stage_order": 2, "stage_key": "product_negative", "label": "产品负面且有原声片段", "item_count": 417, "previous_count": 1000, "retention_pct": 41.7, "loss_tag": None},
    {"row_type": "stage", "stage_order": 3, "stage_key": "innovation_material", "label": "需求缺口 / 竞品对标原料", "item_count": 300, "previous_count": 417, "retention_pct": 71.9, "loss_tag": None},
    {"row_type": "stage", "stage_order": 4, "stage_key": "attached", "label": "已挂靠到机会点", "item_count": 240, "previous_count": 300, "retention_pct": 80.0, "loss_tag": None},
    {"row_type": "stage", "stage_order": 5, "stage_key": "spu_cards", "label": "进入 SPU 卡片层", "item_count": 180, "previous_count": 240, "retention_pct": 75.0, "loss_tag": None},
    {"row_type": "loss_tag", "stage_order": None, "stage_key": None, "label": None, "item_count": 90, "previous_count": None, "retention_pct": None, "loss_tag": "价格高低"},
]


def evidence_rows(*, empty: bool = False):
    result = []
    bins = [
        (1, "one", "1 条"),
        (2, "two", "2 条"),
        (3, "three_five", "3–5 条"),
        (4, "six_ten", "6–10 条"),
        (5, "eleven_plus", "11 条以上"),
    ]
    for lifecycle_order, lifecycle in enumerate(("老品迭代", "新品创新"), 1):
        counts = [0, 0, 0, 0, 0] if empty else (
            [2, 3, 4, 1, 0] if lifecycle == "老品迭代" else [7, 2, 1, 0, 0]
        )
        total = sum(counts)
        for (bucket_order, bucket_key, bucket_label), count in zip(bins, counts):
            result.append({
                "lifecycle_order": lifecycle_order,
                "lifecycle": lifecycle,
                "bucket_order": bucket_order,
                "bucket_key": bucket_key,
                "bucket_label": bucket_label,
                "opportunity_count": count,
                "evidence_count": count * bucket_order,
                "lifecycle_total": total,
                "opportunity_pct": count / total * 100 if total else None,
            })
    return result


def similarity_rows(*, empty: bool = False):
    result = []
    bins = [
        (1, "near_synonym", "< 0.05 · 近乎同义"),
        (2, "highly_similar", "0.05–0.10 · 高度相似"),
        (3, "similar", "0.10–0.15 · 相似"),
        (4, "related", "0.15–0.30 · 相关"),
        (5, "unrelated", "≥ 0.30 · 基本无关"),
    ]
    for lifecycle_order, lifecycle in enumerate(("老品迭代", "新品创新"), 1):
        for bucket_order, bucket_key, bucket_label in bins:
            result.append({
                "row_type": "bucket",
                "lifecycle_order": lifecycle_order,
                "lifecycle": lifecycle,
                "bucket_order": bucket_order,
                "bucket_key": bucket_key,
                "bucket_label": bucket_label,
                "opportunity_count": 0 if empty else bucket_order,
                "vector_count": 0 if empty else 15,
                "opp_id_a": None, "title_a": None, "spu_a": None,
                "opp_id_b": None, "title_b": None, "spu_b": None,
                "distance": None, "pair_order": None,
            })
    if not empty:
        result.append({
            "row_type": "pair", "lifecycle_order": None,
            "lifecycle": "新品创新", "bucket_order": None,
            "bucket_key": None, "bucket_label": None,
            "opportunity_count": None, "vector_count": None,
            "opp_id_a": "INNO-1", "title_a": "磁吸配件生态", "spu_a": None,
            "opp_id_b": "INNO-2", "title_b": "磁吸附件扩展", "spu_b": None,
            "distance": 0.0312, "pair_order": 1,
        })
    return result


def status_rows(*, empty: bool = False):
    statuses = ("考虑中", "在跟进", "项目中", "已完成", "不考虑", "未表态")
    counts = [0, 0, 0, 0, 0, 0] if empty else [2, 1, 1, 1, 0, 5]
    total = sum(counts)
    return [{
        "status_order": order, "status": status, "issue_count": count,
        "issue_total": total, "issue_pct": count / total * 100 if total else None,
    } for order, (status, count) in enumerate(zip(statuses, counts), 1)]


FULL_ROWS = {
    Q.HOME_EVIDENCE_FUNNEL: FUNNEL_ROWS,
    Q.HOME_EVIDENCE_PER_OPP: evidence_rows(),
    Q.HOME_SIMILARITY: similarity_rows(),
    Q.HOME_ISSUE_STATUS: status_rows(),
    Q.HOME_COVERAGE: [{
        "src_line": "社媒", "lang": "de", "message_count": 80,
        "evidence_count": 60, "opportunity_count": 0, "conversion_pct": 0,
    }],
    Q.HOME_FRESHNESS: [
        {"row_type": "freshness", "latest_publish_time": datetime(2026, 8, 16, tzinfo=timezone.utc), "coverage_weeks": 12, "spu_issue_total": 100, "dangling_count": 2, "dangling_pct": 2, "run_id": None, "stage": None, "status": None, "started_at": None, "finished_at": None, "llm_calls": None, "llm_tokens": None, "cost_cny": None, "run_order": None},
        {"row_type": "run", "latest_publish_time": None, "coverage_weeks": None, "spu_issue_total": None, "dangling_count": None, "dangling_pct": None, "run_id": "RUN-1", "stage": "generate", "status": "success", "started_at": datetime(2026, 8, 17, 8, tzinfo=timezone.utc), "finished_at": datetime(2026, 8, 17, 9, tzinfo=timezone.utc), "llm_calls": 12, "llm_tokens": 3456, "cost_cny": None, "run_order": 1},
    ],
}


EMPTY_ROWS = {
    Q.HOME_EVIDENCE_FUNNEL: [
        {**row, "item_count": 0, "previous_count": 0 if row["stage_order"] != 1 else None, "retention_pct": None}
        for row in FUNNEL_ROWS if row["row_type"] == "stage"
    ],
    Q.HOME_EVIDENCE_PER_OPP: evidence_rows(empty=True),
    Q.HOME_SIMILARITY: similarity_rows(empty=True),
    Q.HOME_ISSUE_STATUS: status_rows(empty=True),
    Q.HOME_COVERAGE: [],
    Q.HOME_FRESHNESS: [{
        "row_type": "freshness", "latest_publish_time": None,
        "coverage_weeks": 0, "spu_issue_total": 0, "dangling_count": 0,
        "dangling_pct": None, "run_id": None, "stage": None, "status": None,
        "started_at": None, "finished_at": None, "llm_calls": None,
        "llm_tokens": None, "cost_cny": None, "run_order": None,
    }],
}


class HomeDatabase:
    def __init__(self, rows=None):
        self.rows = rows or FULL_ROWS
        self.calls = []

    def query(self, sql, params=None):
        assert params is None
        self.calls.append(sql)
        if sql not in self.rows:
            raise AssertionError("unexpected query")
        return deepcopy(self.rows[sql])

    def query_one(self, sql, params=None):
        assert sql == Q.SHELL_COUNTS
        return {"iter": 0, "inno": 0, "strategy": 0, "search": 0, "revived": 0}


def make_client(monkeypatch, rows=None):
    fake = HomeDatabase(rows)
    monkeypatch.setattr(home, "db", fake)
    monkeypatch.setattr(web, "db", fake)
    app = FastAPI()
    static_dir = Path(home.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(home.router)
    return TestClient(app), fake


@pytest.mark.parametrize(
    "query, required",
    [
        (Q.HOME_EVIDENCE_FUNNEL, {"row_type", "item_count"}),
        (Q.HOME_EVIDENCE_PER_OPP, {"lifecycle", "bucket_key", "opportunity_count", "evidence_count"}),
        (Q.HOME_SIMILARITY, {"row_type", "lifecycle", "distance"}),
        (Q.HOME_ISSUE_STATUS, {"status", "issue_count"}),
        (Q.HOME_COVERAGE, {"src_line", "lang", "message_count", "evidence_count", "opportunity_count"}),
        (Q.HOME_FRESHNESS, {"row_type", "spu_issue_total", "dangling_count"}),
    ],
)
def test_each_home_query_has_expected_fixture_structure(query, required):
    rows = HomeDatabase().query(query)
    assert rows
    assert required <= rows[0].keys()


def test_home_route_renders_all_six_sections(monkeypatch):
    client, fake = make_client(monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    for text in (
        "证据漏斗", "机会点证据数分布", "语义相似度分布",
        "卡片状态分布", "来源与语种覆盖", "数据新鲜度与运行健康",
    ):
        assert text in response.text
    assert fake.calls == [
        Q.HOME_EVIDENCE_FUNNEL, Q.HOME_EVIDENCE_PER_OPP,
        Q.HOME_SIMILARITY, Q.HOME_ISSUE_STATUS,
        Q.HOME_COVERAGE, Q.HOME_FRESHNESS,
    ]


def test_empty_database_and_empty_manual_and_run_log_still_render(monkeypatch):
    client, _ = make_client(monkeypatch, EMPTY_ROWS)
    response = client.get("/")
    assert response.status_code == 200
    for text in (
        "暂无事实层证据", "暂无有效机会点", "暂无 SPU 问题条目",
        "暂无来源与语种数据", "暂无运行记录", "暂无消息时间",
    ):
        assert text in response.text
    assert "None" not in response.text


def test_dangling_ratio_over_five_percent_shows_alert(monkeypatch):
    rows = deepcopy(FULL_ROWS)
    rows[Q.HOME_FRESHNESS][0].update(
        spu_issue_total=182, dangling_count=159, dangling_pct=87.4
    )
    client, _ = make_client(monkeypatch, rows)
    response = client.get("/")
    assert response.status_code == 200
    assert "派生层悬空占比超过 5%" in response.text
    assert "87.4%" in response.text


def compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_home_sql_contracts_match_pipeline_definitions():
    funnel = compact(Q.HOME_EVIDENCE_FUNNEL)
    assert "e.is_product" in funnel
    assert "e.sentiment = '负面'" in funnel
    assert "nullif(btrim(e.snippet), '') is not null" in funnel
    assert "e.is_product is not true" in funnel
    assert "array['用户咨询', '其他']" in funnel
    assert "array['产品评测', '竞品拉踩']" in funnel
    assert "limit 8" in funnel

    distribution = compact(Q.HOME_EVIDENCE_PER_OPP)
    assert "from voc_opp_evidence" in distribution
    assert "between 6 and 10" in distribution
    assert "evidence_count > 10" in distribution

    coverage = compact(Q.HOME_COVERAGE)
    assert "array['用户使用体验']" in coverage
    assert "not p.requires_spu" in coverage
    assert "count(distinct o.opp_id)" in coverage

    freshness = compact(Q.HOME_FRESHNESS)
    assert "left join voc_opportunity" in freshness
    assert "o.opp_id is null or o.merged_into is not null" in freshness
    assert "metrics -> 'cost' ->> 'cny'" in freshness
    assert "limit 5" in freshness


def test_similarity_reads_nn_cache_not_per_request_knn():
    """相似度必须读 voc_opp_nn 缓存（021 迁移），不得在请求时做 KNN。

    原实现的逐机会点 LATERAL KNN 计划合法，但过滤条件让 HNSW 索引失效，
    真库实测 103 秒（预算 200ms）。缓存由收尾调用 voc_refresh_opp_nn() 刷新。
    """
    sql = compact(Q.HOME_SIMILARITY)
    assert "from voc_opp_nn" in sql
    # 请求路径上禁止任何向量距离计算——它属于批量刷新函数。
    assert "<=>" not in sql
    # 过期行防护：缓存落后于机会点层重建时，指向已删/已合并机会点的行
    # 必须被双侧有效性 JOIN 滤掉，而不是进入统计。
    assert "join active a on a.opp_id = nn.opp_id" in sql
    assert "join active b on b.opp_id = nn.neighbor_id" in sql

    migration = (
        Path(__file__).resolve().parents[2] / "pipeline/sql/021_opp_nn_cache.sql"
    ).read_text()
    assert "CREATE TABLE IF NOT EXISTS voc_opp_nn" in migration
    assert "voc_refresh_opp_nn()" in migration
    assert "GRANT SELECT ON voc_opp_nn TO voc_writer, voc_human, voc_reader" \
        in migration


def test_zero_conversion_with_at_least_fifty_evidence_is_marked(monkeypatch):
    client, _ = make_client(monkeypatch)
    response = client.get("/")
    assert response.status_code == 200
    assert "颗粒无收" in response.text
