"""把数据库行整理成模板需要的稳定视图模型。"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping


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
