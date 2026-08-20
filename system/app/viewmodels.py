"""把数据库行整理成模板需要的稳定视图模型。"""
from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping


INACTIVE_STATUSES = frozenset(("已完成", "不考虑"))


def attach_full_voice(rows: Iterable[Mapping[str, Any]],
                      snippet_key: str = "voice_text",
                      full_key: str = "full_content") -> list[dict]:
    """原声条目补「完整原声」展开：正句显示片段，全文折叠。

    片段能在全文中逐字定位时（实测电商 75%、社媒 50%——外语片段对不上
    中译文），拆成 pre/hit/post 三段供模板做 <mark> 高亮；定位不到就整段
    展开不高亮。译文与原文相同或全文缺失时不产出展开，模板据 full_content
    是否存在决定渲染。"""
    out: list[dict] = []
    for row in rows:
        item = dict(row)
        full = item.get(full_key)
        snip = (item.get(snippet_key) or "").strip()
        if isinstance(full, str) and full.strip() and snip and snip in full:
            pre, _, post = full.partition(snip)
            item["full_pre"], item["full_hit"], item["full_post"] = pre, snip, post
        out.append(item)
    return out


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
        if row.get("has_ec") is None:
            row["has_ec"] = True
        row["negative_ratio"] = negative_ratio(row)
        for key in (
            "issue_count", "open_issue_count", "recent_evi_count", "raw_voice_count"
        ):
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
        card["issues"] = card_issues
        card["top_issue"] = card_issues[0] if card_issues else None
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


HOME2_STATES = ("考虑中", "在跟进", "项目中", "已完成", "不考虑")
HOME2_STATE_TOKENS = {
    "考虑中": "s1", "在跟进": "s2", "项目中": "s3",
    "已完成": "s4", "不考虑": "s5",
}
HOME2_HUES = ("iter", "inno", "s2", "s3", "s1", "s4", "ink-3", "ink-4", "idle")
HOME2_DIST_SOURCES = frozenset(("社媒", "电商", "客服"))
HOME2_DIST_DIMS = frozenset(("语种", "平台"))
HOME2_DANGLING_ALERT_PCT = 5.0

SANKEY_WIDTH = 980.0
SANKEY_HEIGHT = 430.0
SANKEY_PAD = 14.0
SANKEY_SOURCE_GAP = 26.0
SANKEY_TAG_GAP = 9.0
SANKEY_LABEL_GAP = 14.0
SANKEY_NODE_WIDTH = 13.0
SANKEY_X_SOURCE = 0.0
SANKEY_X_TAG = 232.0
SANKEY_X_TAG_OUT = 470.0
SANKEY_X_DEST = 782.0


def _int(value: Any) -> int:
    return int(value or 0)


def _float(value: Any) -> float | None:
    return float(value) if value is not None else None


def _pct(value: int | float, total: int | float) -> float:
    return value / total * 100 if total else 0.0


def _pct_label(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}%"


def _svg_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _time_label(value: Any, pattern: str, empty: str) -> str:
    if value in (None, ""):
        return empty
    if isinstance(value, datetime):
        return value.strftime(pattern)
    return str(value)


def _date_label(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    return str(value)[:10]


def _source_cards(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    notes = {
        "ec": "云听 COMMENT · 按「本品」筛",
        "social": "云听 SOCIAL · 按品牌筛",
        "service": "云听 SERVICE · 尚未接入",
    }
    cards: list[dict[str, Any]] = []
    starts: list[Any] = []
    ends: list[Any] = []
    for source in sorted(rows, key=lambda row: _int(row.get("source_order"))):
        row = dict(source)
        key = str(row.get("source_key") or "")
        used = _int(row.get("used_count"))
        available = (
            None if row.get("available_count") is None
            else _int(row.get("available_count"))
        )
        if available is None:
            rate = None
        elif available > 0:
            rate = used / available * 100
        else:
            rate = 0.0 if used == 0 else None
        cards.append({
            **row,
            "source_key": key,
            "source_label": str(row.get("source_label") or "未知来源"),
            "used_count": used,
            "available_count": available,
            "used_label": format_number(used),
            "available_label": (
                format_number(available) if available is not None else "未探测"
            ),
            "on": key != "service",
            "badge": "已接入" if key != "service" else "未接入",
            "note": notes.get(key, ""),
            "rate": rate,
            # 探针是离线缓存：若 ingest 在探针之后又灌了新数据，分母会过期，
            # 比值可能 >100%（实测出现过 103%）。这时不显示会误导人的百分比，
            # 而是明说分母已过期——真实缺口要靠重跑探针才知道。
            "rate_stale": rate is not None and rate > 100.5,
            "rate_label": (
                "未探测" if rate is None
                else "探针已过期" if rate > 100.5
                else _pct_label(rate, 1 if rate < 1 else 0)
            ),
            "bar_pct": (
                0.0 if available is None
                else 100.0 if rate is not None and rate > 100.5
                else max(rate or 0.0, 0.35)
            ),
        })
        if row.get("window_start") is not None:
            starts.append(row["window_start"])
        if row.get("window_end") is not None:
            ends.append(row["window_end"])
    return {
        "cards": cards,
        "by_key": {card["source_key"]: card for card in cards},
        "window_start_label": _date_label(min(starts)) if starts else None,
        "window_end_label": _date_label(max(ends)) if ends else None,
        "all_probed": bool(cards) and all(
            card["available_count"] is not None for card in cards
        ),
    }


def _flow_rows(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for source in rows:
        row = dict(source)
        item = {
            **row,
            "name": str(row.get("label") or "无标签"),
            "total": _int(row.get("message_count")),
            "iter": _int(row.get("iter_count")),
            "inno": _int(row.get("inno_count")),
        }
        item["entered"] = item["iter"] + item["inno"]
        normalized.append(item)
    return normalized


def _band_path(xa: float, ya: float, ha: float,
               xb: float, yb: float, hb: float) -> str:
    middle = (xa + xb) / 2
    return " ".join((
        f"M{_svg_number(xa)},{_svg_number(ya)}",
        f"C{_svg_number(middle)},{_svg_number(ya)}",
        f"{_svg_number(middle)},{_svg_number(yb)}",
        f"{_svg_number(xb)},{_svg_number(yb)}",
        f"L{_svg_number(xb)},{_svg_number(yb + hb)}",
        f"C{_svg_number(middle)},{_svg_number(yb + hb)}",
        f"{_svg_number(middle)},{_svg_number(ya + ha)}",
        f"{_svg_number(xa)},{_svg_number(ya + ha)} Z",
    ))


def _sankey(ec_rows: list[dict[str, Any]], social_rows: list[dict[str, Any]],
            ec_classes: int, social_classes: int) -> dict[str, Any]:
    source_defs = [
        {
            "name": "电商评论", "sub": f"云听全局标签 · {ec_classes} 类",
            "rows": ec_rows, "color": "iter",
        },
        {
            "name": "社交媒体", "sub": f"云听内容标签 · {social_classes} 类",
            "rows": social_rows, "color": "inno",
        },
    ]
    sources = []
    for source in source_defs:
        rows = [dict(row) for row in source["rows"] if row["entered"] > 0]
        value = sum(row["entered"] for row in rows)
        if value:
            sources.append({**source, "rows": rows, "value": value})

    entered = sum(source["value"] for source in sources)
    tag_count = sum(len(source["rows"]) for source in sources)
    if not entered or not sources:
        return {
            "width": SANKEY_WIDTH, "height": SANKEY_HEIGHT,
            "entered": 0, "entered_label": format_number(0),
            "bands": [], "sources": [], "tags": [], "destinations": [],
            "columns": [],
            "column_height": 0.0, "source_height_sum": 0.0,
            "tag_height_sum": 0.0, "destination_height_sum": 0.0,
            "label_gap": SANKEY_LABEL_GAP, "empty": True,
        }

    usable = (
        SANKEY_HEIGHT - SANKEY_PAD * 2
        - SANKEY_SOURCE_GAP * (len(sources) - 1)
        - SANKEY_TAG_GAP * (tag_count - len(sources))
    )
    scale = max(usable, 0.0) / entered

    source_y = SANKEY_PAD
    for source in sources:
        source["height"] = source["value"] * scale
        source["y"] = source_y
        source_y += source["height"] + SANKEY_SOURCE_GAP

    tag_y = SANKEY_PAD
    flat_tags: list[dict[str, Any]] = []
    for source_index, source in enumerate(sources):
        for row in source["rows"]:
            flow_height = row["entered"] * scale
            # 视觉稿给极细节点 2px 可见高度。流带仍保持线性高度，避免原稿
            # 的 min-height 令来源列凭空多出流量；两者分开后守恒可直接断言。
            node_height = max(flow_height, 2.0)
            tag = {
                **row,
                "source_name": source["name"],
                "source_color": source["color"],
                "y": tag_y,
                "flow_height": flow_height,
                "height": node_height,
            }
            flat_tags.append(tag)
            source.setdefault("tags", []).append(tag)
            tag_y += node_height + SANKEY_TAG_GAP
        if source_index < len(sources) - 1:
            tag_y += SANKEY_SOURCE_GAP - SANKEY_TAG_GAP

    iter_total = sum(tag["iter"] for tag in flat_tags)
    inno_total = sum(tag["inno"] for tag in flat_tags)
    destinations = [
        {"name": "老品迭代", "value": iter_total, "color": "iter"},
        {"name": "新品创新", "value": inno_total, "color": "inno"},
    ]
    dest_y = SANKEY_PAD
    for destination in destinations:
        destination["height"] = destination["value"] * scale
        destination["y"] = dest_y
        destination["pct_label"] = _pct_label(
            _pct(destination["value"], entered), 1)
        dest_y += destination["height"] + SANKEY_SOURCE_GAP
    destination_offsets = {
        destination["name"]: destination["y"] for destination in destinations
    }

    bands: list[dict[str, Any]] = []
    for source in sources:
        offset = source["y"]
        for tag in source.get("tags", []):
            height = tag["flow_height"]
            bands.append({
                "stage": "source_tag",
                "height": height,
                "color": source["color"],
                "opacity": 0.3,
                "d": _band_path(
                    SANKEY_X_SOURCE + SANKEY_NODE_WIDTH, offset, height,
                    SANKEY_X_TAG, tag["y"], height,
                ),
                "tip": (
                    f"{source['name']} → {tag['name']}<br>"
                    f"<b>{format_number(tag['entered'])}</b> 条进入机会点"
                ),
            })
            offset += height

    for tag in flat_tags:
        offset = tag["y"]
        for name, value, color in (
            ("老品迭代", tag["iter"], "iter"),
            ("新品创新", tag["inno"], "inno"),
        ):
            if not value:
                continue
            height = value * scale
            destination_y = destination_offsets[name]
            bands.append({
                "stage": "tag_dest",
                "height": height,
                "color": color,
                "opacity": 0.62,
                "d": _band_path(
                    SANKEY_X_TAG_OUT, offset, height,
                    SANKEY_X_DEST, destination_y, height,
                ),
                "tip": (
                    f"{tag['name']} → {name}<br>"
                    f"<b>{format_number(value)}</b> 条"
                ),
            })
            offset += height
            destination_offsets[name] += height

    source_nodes = []
    for source in sources:
        source_nodes.append({
            **source,
            "x": SANKEY_X_SOURCE, "width": SANKEY_NODE_WIDTH,
            "value_label": format_number(source["value"]),
            "label_y": source["y"] + source["height"] / 2,
            "label_x": SANKEY_X_SOURCE + SANKEY_NODE_WIDTH + 9,
            "title_y": source["y"] + source["height"] / 2 - 6,
            "value_y": source["y"] + source["height"] / 2 + 8,
            "sub_y": source["y"] + source["height"] / 2 + 21,
        })

    last_label_y = -math.inf
    tag_nodes = []
    for tag in flat_tags:
        middle = tag["y"] + tag["height"] / 2
        label_y = max(middle, last_label_y + SANKEY_LABEL_GAP)
        tag_nodes.append({
            **tag,
            "x": SANKEY_X_TAG, "width": SANKEY_NODE_WIDTH,
            "value_label": format_number(tag["entered"]),
            "label_y": label_y,
            "label_x": SANKEY_X_TAG + SANKEY_NODE_WIDTH + 10,
            "text_y": label_y + 3.5,
            "value_x": SANKEY_X_TAG_OUT - 8,
            "leader_d": (
                f"M{_svg_number(SANKEY_X_TAG + SANKEY_NODE_WIDTH + 1)},"
                f"{_svg_number(middle)} "
                f"L{_svg_number(SANKEY_X_TAG + SANKEY_NODE_WIDTH + 7)},"
                f"{_svg_number(label_y)}"
                if abs(label_y - middle) > 2 else None
            ),
        })
        last_label_y = label_y

    destination_nodes = []
    for destination in destinations:
        destination_nodes.append({
            **destination,
            "x": SANKEY_X_DEST, "width": SANKEY_NODE_WIDTH,
            "value_label": format_number(destination["value"]),
            "label_y": destination["y"] + destination["height"] / 2,
            "label_x": SANKEY_X_DEST + SANKEY_NODE_WIDTH + 9,
            "title_y": destination["y"] + destination["height"] / 2 - 5,
            "value_y": destination["y"] + destination["height"] / 2 + 9,
        })

    column_height = entered * scale
    return {
        "width": SANKEY_WIDTH,
        "height": SANKEY_HEIGHT,
        "entered": entered,
        "entered_label": format_number(entered),
        "scale": scale,
        "bands": bands,
        "sources": source_nodes,
        "tags": tag_nodes,
        "destinations": destination_nodes,
        "columns": [
            {"x": SANKEY_X_SOURCE, "y": 9.0, "label": "来源"},
            {"x": SANKEY_X_TAG, "y": 9.0, "label": "各自的标签体系"},
            {"x": SANKEY_X_DEST, "y": 9.0, "label": "生命周期"},
        ],
        "column_height": column_height,
        "source_height_sum": sum(node["height"] for node in source_nodes),
        "tag_height_sum": sum(node["flow_height"] for node in tag_nodes),
        "destination_height_sum": sum(
            node["height"] for node in destination_nodes),
        "label_gap": SANKEY_LABEL_GAP,
        "empty": False,
    }


def _flow(source_cards: dict[str, Any],
          social_input: Iterable[Mapping[str, Any]],
          ec_input: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    social_rows = _flow_rows(social_input)
    ec_rows = _flow_rows(ec_input)
    social_total = sum(row["total"] for row in social_rows)
    ec_total = sum(row["total"] for row in ec_rows)
    social_classes = max(
        (_int(row.get("label_class_count")) for row in social_rows), default=0)
    ec_classes = max(
        (_int(row.get("label_class_count")) for row in ec_rows), default=0)

    service = source_cards["by_key"].get("service", {})
    raw_totals = [
        {
            "name": "电商评论", "total": ec_total,
            "iter": sum(row["iter"] for row in ec_rows),
            "inno": sum(row["inno"] for row in ec_rows), "on": True,
        },
        {
            "name": "社交媒体", "total": social_total,
            "iter": sum(row["iter"] for row in social_rows),
            "inno": sum(row["inno"] for row in social_rows), "on": True,
        },
        {
            "name": "客服会话", "total": _int(service.get("used_count")),
            "iter": 0, "inno": 0, "on": False,
            "available": service.get("available_count"),
            "available_label": service.get("available_label", "未探测"),
        },
    ]
    maximum = max((row["total"] for row in raw_totals if row["on"]), default=0)
    totals = []
    for raw in raw_totals:
        row = dict(raw)
        row["total_label"] = format_number(row["total"])
        row["bar_width_pct"] = (
            _pct(row["total"], maximum) if row["on"] else 100.0
        )
        row["unentered"] = max(row["total"] - row["iter"] - row["inno"], 0)
        segments = []
        for label, value, color, dark in (
            ("老品迭代", row["iter"], "iter", True),
            ("新品创新", row["inno"], "inno", True),
            ("未进入机会点", row["unentered"], "sunk", False),
        ):
            if not value or not row["total"]:
                continue
            percent = _pct(value, row["total"])
            segments.append({
                "label": label, "value": value, "value_label": format_number(value),
                "color": color, "dark_text": dark, "width_pct": percent,
                "show_value": percent > 6,
                "pct_label": _pct_label(percent, 1),
                "tip": (
                    f"{row['name']} → {label}<br>"
                    f"<b>{format_number(value)}</b> 条 · {_pct_label(percent, 1)}"
                ),
            })
        row["segments"] = segments
        totals.append(row)

    sankey = _sankey(ec_rows, social_rows, ec_classes, social_classes)
    source_total = ec_total + social_total
    entered = sankey["entered"]
    entered_pct = _pct(entered, source_total)
    return {
        "totals": totals,
        "sankey": sankey,
        "ec_rows": ec_rows,
        "social_rows": social_rows,
        "ec_class_count": ec_classes,
        "social_class_count": social_classes,
        "source_total": source_total,
        "entered": entered,
        "entered_pct": entered_pct,
        "entered_pct_label": _pct_label(entered_pct, 1),
        "unentered_pct_label": _pct_label(100 - entered_pct, 1),
    }


def _status(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    row_list = [dict(row) for row in rows]
    lifecycles = []
    nondefault = 0
    for order, lifecycle, color, anchor in (
        (1, "老品迭代", "iter", "锚定 SPU × 问题"),
        (2, "新品创新", "inno", "锚定机会点"),
    ):
        indexed = {
            str(row.get("status")): _int(row.get("item_count"))
            for row in row_list
            if row.get("row_type") == "lifecycle"
            and str(row.get("lifecycle")) == lifecycle
        }
        total = sum(indexed.get(state, 0) for state in HOME2_STATES)
        segments = []
        keys = []
        for state in HOME2_STATES:
            value = indexed.get(state, 0)
            percent = _pct(value, total)
            item = {
                "state": state, "value": value, "value_label": format_number(value),
                "token": HOME2_STATE_TOKENS[state], "width_pct": percent,
                "pct_label": _pct_label(percent, 1),
                "tip": (
                    f"{state}<br><b>{format_number(value)}</b> 项 · "
                    f"{_pct_label(percent, 1)}"
                ),
            }
            keys.append(item)
            if value:
                segments.append(item)
            if state != "考虑中":
                nondefault += value
        lifecycles.append({
            "order": order, "lifecycle": lifecycle, "color": color,
            "anchor": anchor, "total": total, "total_label": format_number(total),
            "segments": segments, "status_keys": keys,
        })

    machine = []
    for row in sorted(
        (row for row in row_list if row.get("row_type") == "machine"),
        key=lambda row: _int(row.get("machine_order")),
    ):
        value = _int(row.get("item_count"))
        label = str(row.get("machine_label") or "未记录")
        machine.append({
            "label": label, "value": value, "value_label": format_number(value),
            "warn": label == "待复核",
        })
    return {"lifecycles": lifecycles, "machine": machine, "cold_start": nondefault == 0}


def _freshness(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    source = dict(next(iter(rows), {}))
    dangling = _int(source.get("dangling_count"))
    dangling_total = _int(source.get("dangling_total"))
    dangling_pct = _float(source.get("dangling_pct"))
    if dangling_pct is None:
        dangling_pct = _pct(dangling, dangling_total)
    ledger_count = (
        None if source.get("ledger_item_count") is None
        else _int(source.get("ledger_item_count"))
    )
    return {
        "latest_publish_label": _time_label(
            source.get("latest_publish_time"), "%Y-%m-%d %H:%M", "暂无消息时间"),
        # 日期与时刻分开给：卡片里日期是主角，时刻只做弱化后缀，
        # 合成一串在窄列里会折行，把「08-17」和「00:00」劈成两行。
        "latest_publish_short_label": _time_label(
            source.get("latest_publish_time"), "%m-%d", "暂无"),
        "latest_publish_clock_label": _time_label(
            source.get("latest_publish_time"), "%H:%M", ""),
        "coverage_weeks": _int(source.get("coverage_weeks")),
        "spu_count": _int(source.get("spu_count")),
        "spu_issue_total": _int(source.get("spu_issue_total")),
        "spu_with_issue_count": _int(source.get("spu_with_issue_count")),
        "dangling_count": dangling,
        "dangling_total": dangling_total,
        "dangling_pct": dangling_pct,
        "dangling_pct_label": _pct_label(dangling_pct, 1),
        "dangling_alert": dangling_total > 0 and dangling_pct > HOME2_DANGLING_ALERT_PCT,
        "generation_id": source.get("generation_id"),
        "generation_week": source.get("generation_week"),
        "ledger_complete": bool(source.get("ledger_complete")),
        "ledger_item_count": ledger_count,
    }


def _weekly(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    weekly = [dict(row) for row in rows]
    weekly.sort(key=lambda row: _int(row.get("week_order")))
    width, height, pad = 520.0, 96.0, 3.0
    count = len(weekly)
    bar_width = (width - pad * 2) / count if count else 0.0
    maximum = max(
        (_int(row.get("social_count")) + _int(row.get("ec_count")) for row in weekly),
        default=0,
    )
    rects = []
    zero_weeks = 0
    for index, source in enumerate(weekly):
        social = _int(source.get("social_count"))
        ec = _int(source.get("ec_count"))
        if social + ec == 0:
            zero_weeks += 1
        x = pad + index * bar_width
        ec_height = ec / maximum * (height - 12) if maximum else 0.0
        social_height = social / maximum * (height - 12) if maximum else 0.0
        tip = (
            f"{source.get('week_label') or '未标注周'}<br>"
            f"社媒 <b>{format_number(social)}</b> · "
            f"电商 <b>{format_number(ec)}</b>"
        )
        rects.extend((
            {
                "x": x + 0.7, "y": height - ec_height,
                "width": max(bar_width - 1.6, 0.0), "height": ec_height,
                "color": "iter", "opacity": 0.72, "tip": tip,
            },
            {
                "x": x + 0.7, "y": height - ec_height - social_height,
                "width": max(bar_width - 1.6, 0.0), "height": social_height,
                "color": "ink-3", "opacity": 0.4, "tip": tip,
            },
        ))
    return {
        "width": width, "height": height, "rects": rects,
        "zero_weeks": zero_weeks, "week_count": count,
        "note": (
            f"{zero_weeks} 个断周" if zero_weeks else f"{count} 周无断周"
        ),
        "first_label": str(weekly[0].get("week_label") or "") if weekly else "",
        "last_label": str(weekly[-1].get("week_label") or "") if weekly else "",
    }


def _donut_arc(cx: float, cy: float, radius: float,
               start: float, angle: float) -> str:
    def point(value: float) -> tuple[float, float]:
        return cx + radius * math.cos(value), cy + radius * math.sin(value)

    x0, y0 = point(start)
    if math.isclose(angle, math.tau, rel_tol=0.0, abs_tol=1e-9):
        xm, ym = point(start + math.pi)
        return (
            f"M{_svg_number(x0)},{_svg_number(y0)} "
            f"A{_svg_number(radius)},{_svg_number(radius)} 0 1,1 "
            f"{_svg_number(xm)},{_svg_number(ym)} "
            f"A{_svg_number(radius)},{_svg_number(radius)} 0 1,1 "
            f"{_svg_number(x0)},{_svg_number(y0)}"
        )
    x1, y1 = point(start + angle)
    return (
        f"M{_svg_number(x0)},{_svg_number(y0)} "
        f"A{_svg_number(radius)},{_svg_number(radius)} 0 "
        f"{1 if angle > math.pi else 0},1 {_svg_number(x1)},{_svg_number(y1)}"
    )


def normalize_home_dist(rows: Iterable[Mapping[str, Any]], *,
                        src: str, dim: str) -> dict[str, Any]:
    """把一个源×维度分布变成局部模板可直接输出的环形几何。"""
    if src not in HOME2_DIST_SOURCES or dim not in HOME2_DIST_DIMS:
        src, dim = "社媒", "语种"
    if src == "客服":
        return {
            "src": src, "dim": dim, "empty": True,
            "empty_message": "客服会话尚未接入，无分布数据", "arcs": [], "rows": [],
        }
    if src == "电商" and dim == "语种":
        return {
            "src": src, "dim": dim, "empty": True,
            "empty_message": "电商侧云听未回填语种字段，全部为「未标注」",
            "arcs": [], "rows": [],
        }

    normalized = []
    for source in rows:
        row = dict(source)
        value = _int(row.get("item_count"))
        if value <= 0:
            continue
        normalized.append({
            "label": str(row.get("label") or "未标注"),
            "value": value,
            "order": _int(row.get("item_order")),
        })
    normalized.sort(key=lambda row: row["order"])
    total = sum(row["value"] for row in normalized)
    if not total:
        return {
            "src": src, "dim": dim, "empty": True,
            "empty_message": "暂无分布数据", "arcs": [], "rows": [],
        }

    angle = -math.pi / 2
    arcs = []
    for index, row in enumerate(normalized):
        sweep = row["value"] / total * math.tau
        percent = _pct(row["value"], total)
        row.update({
            "value_label": format_number(row["value"]),
            "pct": percent,
            "pct_label": _pct_label(percent, 1),
            "color": HOME2_HUES[index % len(HOME2_HUES)],
            "opacity": 0.55 if index > 5 else 0.92,
            "index": index,
        })
        arcs.append({
            **row,
            "d": _donut_arc(59.0, 59.0, 44.0, angle, sweep),
        })
        angle += sweep
    return {
        "src": src, "dim": dim, "empty": False, "empty_message": None,
        "total": total, "total_label": format_number(total),
        "center_pct_label": _pct_label(normalized[0]["pct"], 0),
        "center_label": normalized[0]["label"],
        "arcs": arcs, "rows": normalized,
    }


def normalize_home_v2(
    *,
    source_rows: Iterable[Mapping[str, Any]],
    social_rows: Iterable[Mapping[str, Any]],
    ec_rows: Iterable[Mapping[str, Any]],
    status_rows: Iterable[Mapping[str, Any]],
    freshness_rows: Iterable[Mapping[str, Any]],
    weekly_rows: Iterable[Mapping[str, Any]],
    dist_rows: Iterable[Mapping[str, Any]],
    dist_src: str = "社媒",
    dist_dim: str = "语种",
) -> dict[str, Any]:
    """把首页七条查询归一成模板直接输出的数据与全部 SVG 几何。"""
    sources = _source_cards(source_rows)
    freshness = _freshness(freshness_rows)
    return {
        "sources": sources,
        "flow": _flow(sources, social_rows, ec_rows),
        "status": _status(status_rows),
        "freshness": freshness,
        "weekly": _weekly(weekly_rows),
        "dist": normalize_home_dist(dist_rows, src=dist_src, dim=dist_dim),
        "header": {
            "generation_id": freshness["generation_id"],
            "generation_week": freshness["generation_week"],
            "ledger_complete": freshness["ledger_complete"],
            "ledger_item_count": freshness["ledger_item_count"],
            "coverage_weeks": freshness["coverage_weeks"],
            "latest_publish_label": freshness["latest_publish_label"],
        },
    }
