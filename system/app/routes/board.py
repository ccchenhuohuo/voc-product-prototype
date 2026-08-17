from __future__ import annotations

from fastapi import APIRouter, Query, Request

from .. import queries as Q
from ..db import db
from ..viewmodels import group_board
from ..web import templates

router = APIRouter()
SORT_KEYS = frozenset(("grade", "ratio", "evidence", "star", "issues"))


def _sort_state(sort: str, direction: str) -> tuple[str, str]:
    key = sort.strip()
    value = direction.strip().lower()
    return (key, value) if key in SORT_KEYS and value in ("asc", "desc") else ("", "")


@router.get("/iter")
def iteration_queue(
    request: Request,
    queue_filter: str = Query("", alias="filter"),
    sort: str = Query(""),
    dir: str = Query(""),
):
    selected_filter = "revived" if queue_filter.strip() == "revived" else ""
    sort_key, sort_dir = _sort_state(sort, dir)
    result_query = Q.BOARD_SPUS_REVIVED if selected_filter else Q.BOARD_SPUS
    cards = group_board(
        db.query(result_query, (sort_key, sort_dir)),
        db.query(Q.BOARD_ISSUES),
        preserve_spu_order=True,
    )
    return templates.TemplateResponse(request, "board.html", {
        "cards": cards,
        "selected_filter": selected_filter,
        "sorting": {"key": sort_key, "dir": sort_dir},
        "max_negative": max(
            (int(card.get("negative_evi_count") or 0) for card in cards),
            default=0,
        ),
    })
