from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request

from .. import queries as Q
from ..db import db
from ..web import templates

router = APIRouter()


def _generation() -> str:
    value = os.environ.get("VOC_STRATEGY_GENERATION", "OPP2-").strip()
    return value if value in ("OPP-", "OPP2-") else "OPP2-"


def _sort_state(axis_type: str, key: str, direction: str) -> tuple[str, str]:
    allowed = {"n_spu", "evidence"} if axis_type == "通病" else {"evidence"}
    clean_key = key.strip()
    clean_dir = direction.strip().lower()
    if clean_key in allowed and clean_dir in ("asc", "desc"):
        return clean_key, clean_dir
    return "", ""


def _top_spus(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _decorate_axes(rows: list[dict], axis_type: str) -> list[dict]:
    result = [dict(row) for row in rows]
    if axis_type == "通病":
        maximum = max((int(row.get("n_spu") or 0) for row in result), default=0)
        for row in result:
            row["top_spus"] = _top_spus(row.get("top_spus"))
            row["spu_bar_pct"] = (
                float(row.get("n_spu") or 0) * 100 / maximum if maximum else 0
            )
            row["eff_bar_pct"] = (
                float(row.get("n_eff") or 0) * 100 / maximum if maximum else 0
            )
    else:
        maximum = max((int(row.get("evi_total") or 0) for row in result), default=0)
        for row in result:
            row["voice_bar_pct"] = (
                int(row.get("evi_total") or 0) * 100 / maximum if maximum else 0
            )
    return result


@router.get("/strategy")
def strategy(
    request: Request,
    axis_type: Literal["通病", "诉求"] = Query("通病", alias="type"),
    sort: str = Query(""),
    dir: str = Query(""),
):
    sort_key, sort_dir = _sort_state(axis_type, sort, dir)
    generation = _generation()
    problem_sort = (sort_key, sort_dir) if axis_type == "通病" else ("", "")
    theme_sort = (sort_key, sort_dir) if axis_type == "诉求" else ("", "")
    problem_axes = _decorate_axes(
        db.query(Q.STRATEGY_AXES, (*problem_sort, generation, "通病")),
        "通病",
    )
    theme_axes = _decorate_axes(
        db.query(Q.STRATEGY_AXES, (*theme_sort, generation, "诉求")),
        "诉求",
    )
    return templates.TemplateResponse(request, "strategy.html", {
        "axes": problem_axes if axis_type == "通病" else theme_axes,
        "problem_count": len(problem_axes),
        "theme_count": len(theme_axes),
        "axis_type": axis_type,
        "sorting": {"key": sort_key, "dir": sort_dir},
    })


@router.get("/strategy/axis/{axis_id}")
def strategy_axis(request: Request, axis_id: str):
    generation = _generation()
    raw_axis = db.query_one(Q.STRATEGY_AXIS, (generation, axis_id))
    if not raw_axis:
        raise HTTPException(404, "未找到该战略轴")
    axis = dict(raw_axis)
    axis["top_spus"] = _top_spus(axis.get("top_spus"))
    maximum = max(
        (int(row.get("evi") or 0) for row in axis["top_spus"]),
        default=0,
    )
    for row in axis["top_spus"]:
        row["bar_pct"] = int(row.get("evi") or 0) * 100 / maximum if maximum else 0
    members = [
        dict(row)
        for row in db.query(
            Q.STRATEGY_AXIS_MEMBERS, (generation, axis_id)
        )
    ]
    return templates.TemplateResponse(request, "strategy_axis.html", {
        "axis": axis,
        "members": members,
    })
