from __future__ import annotations

from fastapi import APIRouter, Query, Request

from .. import queries as Q
from ..db import db
from ..viewmodels import group_board
from ..web import templates
from .spu_table import sort_state

router = APIRouter()


def _keyword_params(keyword: str) -> tuple[str, str, str, str]:
    pattern = f"%{keyword}%"
    return keyword, pattern, pattern, pattern


@router.get("/iter")
def iteration_queue(
    request: Request,
    queue_filter: str = Query("tracked", alias="filter"),
    q: str = Query(""),
    category: str = Query(""),
    sort: str = Query(""),
    dir: str = Query(""),
):
    selected_filter = (
        queue_filter.strip() if queue_filter.strip() in ("revived", "all")
        else "tracked"
    )
    keyword = q.strip()
    selected_category = category.strip()
    keyword_params = _keyword_params(keyword)
    filter_params = (*keyword_params, selected_category)
    sort_key, sort_dir = sort_state(sort, dir)

    base_params = (selected_filter, *filter_params, sort_key, sort_dir)
    result_rows = db.query(
        Q.BOARD_SPUS,
        base_params,
    )
    metadata = result_rows[0] if result_rows else {}
    cards = group_board(
        (row for row in result_rows if row.get("spu")),
        db.query(Q.BOARD_ISSUES),
        preserve_spu_order=True,
    )

    category_counts = {
        str(value): int(count or 0)
        for value, count in dict(metadata.get("category_facets") or {}).items()
    }
    categories = sorted(value for value in category_counts if value != "__all__")
    if selected_category and selected_category not in categories:
        categories.append(selected_category)

    return templates.TemplateResponse(request, "board.html", {
        "cards": cards,
        "selected_filter": selected_filter,
        "filters": {
            "q": keyword,
            "category": selected_category,
        },
        "facets": {"category": category_counts},
        "categories": categories,
        "sorting": {"key": sort_key, "dir": sort_dir},
        "max_negative": max(
            (int(card.get("negative_evi_count") or 0) for card in cards),
            default=0,
        ),
    })
