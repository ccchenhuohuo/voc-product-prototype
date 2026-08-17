"""把数据库行整理成模板需要的稳定视图模型。"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping
from urllib.parse import quote


INACTIVE_STATUSES = frozenset(("已完成", "不考虑"))


def negative_ratio(row: Mapping[str, Any]) -> float:
    """负面证据在正负证据中的占比；空分母按 0 展示。"""
    if row.get("negative_ratio") is not None:
        return float(row["negative_ratio"])
    negative = float(row.get("negative_evi_count") or 0)
    positive = float(row.get("positive_evi_count") or 0)
    total = negative + positive
    return negative / total if total else 0.0


def spu_sort_key(row: Mapping[str, Any]) -> tuple[float, str]:
    return (-negative_ratio(row), str(row.get("spu") or ""))


def is_inactive(status: Any) -> bool:
    return str(status or "") in INACTIVE_STATUSES


def display_status(status: Any, revived_at: Any = None) -> str:
    value = str(status or "考虑中")
    return "待复议" if value == "考虑中" and revived_at is not None else value


def status_mark(status: Any, revived_at: Any = None) -> str:
    shown = display_status(status, revived_at)
    if shown == "待复议":
        return "mk-rev"
    if shown == "考虑中":
        return "mk-todo"
    if shown in ("在跟进", "项目中"):
        return "mk-wip"
    return "mk-none"


def issue_sort_key(row: Mapping[str, Any]) -> tuple[int, int, int, str]:
    """已完成/不考虑沉底，待复议置顶，同组证据数降序。"""
    inactive = is_inactive(row.get("status"))
    revived = row.get("revived_at") is not None and row.get("status") == "考虑中"
    count = int(row.get("evi_count") or 0)
    return (1 if inactive else 0, 0 if revived else 1, -count, str(row.get("opp_id") or ""))


def normalize_spus(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row["negative_ratio"] = negative_ratio(row)
        for key in ("issue_count", "open_issue_count", "recent_evi_count"):
            if key in row:
                row[key] = int(row.get(key) or 0)
        result.append(row)
    return result


def sort_spus(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return normalize_spus(sorted(rows, key=spu_sort_key))


def breadcrumb_from_path(path: str | None) -> list[str]:
    """仅作历史数据兼容；新查询直接读 tax_domain/sub/leaf。"""
    if not path:
        return []
    parts = [part.strip() for part in path.split("/")]
    return [part for part in parts[2:5] if part]


def decorate_issue(row: dict[str, Any]) -> dict[str, Any]:
    if row.get("status") != "考虑中":
        row["revived_at"] = None
    taxonomy = [row.get(key) for key in ("tax_domain", "tax_sub", "tax_leaf")]
    row["breadcrumb"] = [str(part) for part in taxonomy if part]
    if not row["breadcrumb"]:
        row["breadcrumb"] = breadcrumb_from_path(row.get("tax_path"))
    row["display_title"] = row.get("title") or row.get("problem_mode") or row.get("opp_id")
    row["display_status"] = display_status(row.get("status"), row.get("revived_at"))
    row["mark_class"] = status_mark(row.get("status"), row.get("revived_at"))
    row["inactive"] = is_inactive(row.get("status"))
    row["recent_evi_count"] = int(row.get("recent_evi_count") or 0)
    return row


def sort_issues(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [decorate_issue(dict(row)) for row in sorted(rows, key=issue_sort_key)]


def group_board(
    spus: Iterable[Mapping[str, Any]],
    issues: Iterable[Mapping[str, Any]],
    *,
    preserve_spu_order: bool = False,
) -> list[dict[str, Any]]:
    by_spu: dict[str, list[dict[str, Any]]] = {}
    for issue in issues:
        by_spu.setdefault(str(issue["spu"]), []).append(dict(issue))

    result: list[dict[str, Any]] = []
    cards = normalize_spus(spus) if preserve_spu_order else sort_spus(spus)
    for card in cards:
        card_issues = sort_issues(by_spu.get(str(card["spu"]), []))
        if not card_issues:
            continue
        card["issues"] = card_issues
        card["top_issue"] = card_issues[0]
        # SQL 口径严格只算当前 voc_spu_issue；仅在纯函数/测试
        # 调用没有带聚合列时，才从附带问题补默认值。
        if card.get("recent_evi_count") is None:
            card["recent_evi_count"] = sum(row["recent_evi_count"] for row in card_issues)
        if card.get("issue_count") is None:
            card["issue_count"] = len(card_issues)
        if card.get("open_issue_count") is None:
            card["open_issue_count"] = sum(not row["inactive"] for row in card_issues)
        result.append(card)
    return result


def normalize_strategy_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """条形只以当前筛选结果的最大 SPU 数为分母。"""
    result = [dict(row) for row in rows]
    maximum = max((int(row.get("spu_count") or 0) for row in result), default=0)
    for row in result:
        count = int(row.get("spu_count") or 0)
        row["spu_count"] = count
        row["spu_bar_pct"] = count / maximum * 100 if maximum else 0.0
    return result


def product_name(row: Mapping[str, Any]) -> str:
    names = row.get("product_names") or []
    return names[0] if names else str(row.get("spu") or "未知产品")


def format_number(value: Any) -> str:
    if value is None:
        return "—"
    # numeric 列（如 n_eff）经 psycopg 取回来是 Decimal 而不是 float。只判 float
    # 会让它落到最后一行原样输出，页面上就是 16.2537313432835821 这种一长串。
    if isinstance(value, (float, Decimal)):
        return f"{value:,.2f}".rstrip("0").rstrip(".")
    return f"{value:,}"


def voice_summary(voices: list[Mapping[str, Any]]) -> dict[str, Any]:
    stars = [float(row["star"]) for row in voices if row.get("star") is not None]
    countries = Counter(str(row.get("country") or "未知") for row in voices)
    dates = [
        row["publish_time"].date()
        if isinstance(row.get("publish_time"), datetime)
        else row.get("publish_time")
        for row in voices
        if row.get("publish_time")
    ]
    return {
        "count": len(voices),
        "avg_star": round(sum(stars) / len(stars), 1) if stars else None,
        "countries": countries.most_common(),
        "date_min": min(dates) if dates else None,
        "date_max": max(dates) if dates else None,
    }


def date_input(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


HOME_LIFECYCLES = ("老品迭代", "新品创新")


def _as_int(value: Any) -> int:
    return int(value or 0)


def _as_float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _bar_pct(value: int, maximum: int) -> float:
    return value / maximum * 100 if maximum else 0.0


def _percent_label(value: Any, *, empty: str = "0%") -> str:
    number = _as_float(value)
    if number is None:
        return empty
    return f"{number:.2f}".rstrip("0").rstrip(".") + "%"


def _time_label(value: Any, *, empty: str = "未记录") -> str:
    if value is None or value == "":
        return empty
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def normalize_home_funnel(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    stages: list[dict[str, Any]] = []
    loss_tags: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        row["item_count"] = _as_int(row.get("item_count"))
        if row.get("row_type") == "stage":
            row["previous_count"] = (
                _as_int(row.get("previous_count"))
                if row.get("previous_count") is not None else None
            )
            row["retention_pct"] = _as_float(row.get("retention_pct"))
            if row.get("stage_order") == 1:
                row["retention_label"] = "全库起点"
            elif row["previous_count"] == 0:
                row["retention_label"] = "上一层为 0，无法计算"
            else:
                row["retention_label"] = _percent_label(row["retention_pct"])
            stages.append(row)
        elif row.get("row_type") == "loss_tag":
            row["loss_tag"] = str(row.get("loss_tag") or "未标注")
            loss_tags.append(row)

    stages.sort(key=lambda row: _as_int(row.get("stage_order")))
    maximum = max((row["item_count"] for row in stages), default=0)
    for row in stages:
        row["bar_pct"] = _bar_pct(row["item_count"], maximum)
    loss_maximum = max((row["item_count"] for row in loss_tags), default=0)
    for row in loss_tags:
        row["bar_pct"] = _bar_pct(row["item_count"], loss_maximum)

    fact_total = next(
        (row["item_count"] for row in stages if row.get("stage_key") == "fact_total"),
        0,
    )
    return {"stages": stages, "loss_tags": loss_tags, "empty": fact_total == 0}


def normalize_home_evidence_distribution(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    panels = {
        lifecycle: {
            "lifecycle": lifecycle,
            "bins": [],
            "total": 0,
            "evidence_total": 0,
            "single_count": 0,
            "single_pct": 0.0,
        }
        for lifecycle in HOME_LIFECYCLES
    }
    all_bins: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        lifecycle = str(row.get("lifecycle") or "")
        if lifecycle not in panels:
            continue
        row["opportunity_count"] = _as_int(row.get("opportunity_count"))
        row["evidence_count"] = _as_int(row.get("evidence_count"))
        row["opportunity_pct"] = _as_float(row.get("opportunity_pct")) or 0.0
        panels[lifecycle]["bins"].append(row)
        all_bins.append(row)

    maximum = max((row["opportunity_count"] for row in all_bins), default=0)
    for lifecycle, panel in panels.items():
        panel["bins"].sort(key=lambda row: _as_int(row.get("bucket_order")))
        panel["total"] = sum(row["opportunity_count"] for row in panel["bins"])
        panel["evidence_total"] = sum(row["evidence_count"] for row in panel["bins"])
        for row in panel["bins"]:
            row["bar_pct"] = _bar_pct(row["opportunity_count"], maximum)
            if row.get("bucket_key") == "one":
                panel["single_count"] = row["opportunity_count"]
                panel["single_pct"] = (
                    row["opportunity_count"] / panel["total"] * 100
                    if panel["total"] else 0.0
                )
        panel["single_pct_label"] = _percent_label(panel["single_pct"])

    return {"panels": list(panels.values()), "empty": not any(
        panel["total"] for panel in panels.values()
    )}


def _opportunity_url(lifecycle: str, opp_id: str, spu: Any) -> str | None:
    encoded_opp = quote(opp_id, safe="")
    if lifecycle == "新品创新":
        return f"/inno/{encoded_opp}"
    if lifecycle == "老品迭代" and spu:
        return f"/issue/{quote(str(spu), safe='')}/{encoded_opp}"
    return None


def normalize_home_similarity(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    panels = {
        lifecycle: {"lifecycle": lifecycle, "bins": [], "vector_count": 0}
        for lifecycle in HOME_LIFECYCLES
    }
    pairs: list[dict[str, Any]] = []
    all_bins: list[dict[str, Any]] = []
    for source in rows:
        row = dict(source)
        lifecycle = str(row.get("lifecycle") or "")
        if row.get("row_type") == "bucket" and lifecycle in panels:
            row["opportunity_count"] = _as_int(row.get("opportunity_count"))
            row["vector_count"] = _as_int(row.get("vector_count"))
            panels[lifecycle]["bins"].append(row)
            panels[lifecycle]["vector_count"] = row["vector_count"]
            all_bins.append(row)
        elif row.get("row_type") == "pair":
            row["distance"] = _as_float(row.get("distance")) or 0.0
            row["distance_label"] = f"{row['distance']:.4f}"
            row["title_a"] = str(row.get("title_a") or row.get("opp_id_a") or "未命名")
            row["title_b"] = str(row.get("title_b") or row.get("opp_id_b") or "未命名")
            row["url_a"] = _opportunity_url(
                lifecycle, str(row.get("opp_id_a") or ""), row.get("spu_a")
            )
            row["url_b"] = _opportunity_url(
                lifecycle, str(row.get("opp_id_b") or ""), row.get("spu_b")
            )
            pairs.append(row)

    maximum = max((row["opportunity_count"] for row in all_bins), default=0)
    for panel in panels.values():
        panel["bins"].sort(key=lambda row: _as_int(row.get("bucket_order")))
        for row in panel["bins"]:
            row["bar_pct"] = _bar_pct(row["opportunity_count"], maximum)
    pairs.sort(key=lambda row: (_as_int(row.get("pair_order")), row["distance"]))
    return {
        "panels": list(panels.values()),
        "pairs": pairs,
        "empty": not any(panel["vector_count"] for panel in panels.values()),
    }


def normalize_home_issue_status(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    statuses = []
    for source in rows:
        row = dict(source)
        row["status"] = str(row.get("status") or "未表态")
        row["issue_count"] = _as_int(row.get("issue_count"))
        row["issue_pct"] = _as_float(row.get("issue_pct")) or 0.0
        statuses.append(row)
    statuses.sort(key=lambda row: _as_int(row.get("status_order")))
    total = sum(row["issue_count"] for row in statuses)
    maximum = max((row["issue_count"] for row in statuses), default=0)
    for row in statuses:
        row["bar_pct"] = _bar_pct(row["issue_count"], maximum)
        row["issue_pct_label"] = _percent_label(row["issue_pct"])
    return {"statuses": statuses, "total": total, "empty": total == 0}


def normalize_home_coverage(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    coverage = []
    for source in rows:
        row = dict(source)
        row["src_line"] = str(row.get("src_line") or "未标注")
        row["lang"] = str(row.get("lang") or "未标注")
        row["message_count"] = _as_int(row.get("message_count"))
        row["evidence_count"] = _as_int(row.get("evidence_count"))
        row["opportunity_count"] = _as_int(row.get("opportunity_count"))
        row["conversion_pct"] = _as_float(row.get("conversion_pct")) or 0.0
        row["conversion_label"] = _percent_label(row["conversion_pct"])
        row["zero_alert"] = (
            row["evidence_count"] >= 50 and row["opportunity_count"] == 0
        )
        coverage.append(row)
    return {"rows": coverage, "empty": len(coverage) == 0}


def normalize_home_freshness(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    row_list = [dict(row) for row in rows]
    summary_source = next(
        (row for row in row_list if row.get("row_type") == "freshness"), {}
    )
    runs = []
    for source in row_list:
        if source.get("row_type") != "run":
            continue
        row = dict(source)
        row["stage"] = str(row.get("stage") or "未记录")
        row["status"] = str(row.get("status") or "未记录")
        row["started_label"] = _time_label(row.get("started_at"))
        row["finished_label"] = _time_label(row.get("finished_at"))
        row["llm_calls_label"] = (
            format_number(row.get("llm_calls"))
            if row.get("llm_calls") is not None else "未记录"
        )
        row["llm_tokens_label"] = (
            format_number(row.get("llm_tokens"))
            if row.get("llm_tokens") is not None else "未记录"
        )
        row["cost_label"] = (
            "未记录" if row.get("cost_cny") in (None, "")
            else str(row["cost_cny"])
        )
        runs.append(row)
    runs.sort(key=lambda row: _as_int(row.get("run_order")))

    total = _as_int(summary_source.get("spu_issue_total"))
    dangling = _as_int(summary_source.get("dangling_count"))
    dangling_pct = (
        _as_float(summary_source.get("dangling_pct"))
        if summary_source.get("dangling_pct") is not None
        else (dangling / total * 100 if total else 0.0)
    )
    summary = {
        "latest_publish_label": _time_label(
            summary_source.get("latest_publish_time"), empty="暂无消息时间"
        ),
        "coverage_weeks": _as_int(summary_source.get("coverage_weeks")),
        "spu_issue_total": total,
        "dangling_count": dangling,
        "dangling_pct": dangling_pct or 0.0,
        "dangling_pct_label": _percent_label(dangling_pct),
        "dangling_alert": total > 0 and (dangling_pct or 0.0) > 5.0,
    }
    return {"summary": summary, "runs": runs, "runs_empty": len(runs) == 0}


def normalize_home_dashboard(
    *,
    funnel_rows: Iterable[Mapping[str, Any]],
    evidence_rows: Iterable[Mapping[str, Any]],
    similarity_rows: Iterable[Mapping[str, Any]],
    status_rows: Iterable[Mapping[str, Any]],
    coverage_rows: Iterable[Mapping[str, Any]],
    freshness_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """把六条首页查询归一化成模板不需要再运算的稳定结构。"""
    return {
        "funnel": normalize_home_funnel(funnel_rows),
        "evidence_distribution": normalize_home_evidence_distribution(evidence_rows),
        "similarity": normalize_home_similarity(similarity_rows),
        "issue_status": normalize_home_issue_status(status_rows),
        "coverage": normalize_home_coverage(coverage_rows),
        "freshness": normalize_home_freshness(freshness_rows),
    }
