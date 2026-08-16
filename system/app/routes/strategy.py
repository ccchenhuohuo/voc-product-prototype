from __future__ import annotations

from collections import Counter
from typing import Literal

from fastapi import APIRouter, Query, Request

from .. import queries as Q
from ..db import db
from ..viewmodels import normalize_strategy_rows
from ..web import templates

router = APIRouter()


@router.get("/strategy")
def strategy(
    request: Request,
    scope: Literal["品线级", "多品", "单品", "全部"] = Query("品线级"),
):
    all_rows = db.query(Q.STRATEGY_OPPORTUNITIES)
    counts = Counter(str(row.get("scope") or "未分类") for row in all_rows)
    filtered = all_rows if scope == "全部" else [
        row for row in all_rows if row.get("scope") == scope
    ]
    opportunities = normalize_strategy_rows(filtered)
    return templates.TemplateResponse(request, "strategy.html", {
        "opportunities": opportunities,
        "counts": counts,
        "total_count": len(all_rows),
        "scope": scope,
        "selected_scope": scope,
    })
