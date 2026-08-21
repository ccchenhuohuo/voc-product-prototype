from __future__ import annotations

from fastapi import APIRouter, Request

from .. import queries as Q
from ..db import db
from ..viewmodels import (
    HOME2_DIST_DIMS,
    HOME2_DIST_SOURCES,
    normalize_home_dist,
    normalize_home_v2,
)
from ..web import templates

router = APIRouter()


def _dist_selection(src: str, dim: str) -> tuple[str, str]:
    if src not in HOME2_DIST_SOURCES or dim not in HOME2_DIST_DIMS:
        return "社媒", "语种"
    return src, dim


@router.get("/")
def home(request: Request):
    dashboard = normalize_home_v2(
        source_rows=db.query(Q.HOME2_SOURCES),
        social_rows=db.query(Q.HOME2_FLOW_SOCIAL),
        ec_rows=db.query(Q.HOME2_FLOW_EC),
        status_rows=db.query(Q.HOME2_STATUS),
        freshness_rows=db.query(Q.HOME2_FRESHNESS),
        weekly_rows=db.query(Q.HOME2_WEEKLY),
        dist_rows=db.query(Q.HOME2_DIST, ("社媒", "语种")),
    )
    # strategy 读数已切到纯派生轴表；首页一次取齐侧栏，避免再读旧 scope。
    dashboard["nav_counts"] = db.query_one(Q.SHELL_COUNTS) or {}
    return templates.TemplateResponse(request, "home.html", dashboard)


@router.get("/home/dist")
def home_dist(request: Request, src: str = "社媒", dim: str = "语种"):
    selected_src, selected_dim = _dist_selection(src, dim)
    dist = normalize_home_dist(
        db.query(Q.HOME2_DIST, (selected_src, selected_dim)),
        src=selected_src,
        dim=selected_dim,
    )
    return templates.TemplateResponse(
        request, "_home_dist.html", {"dist": dist},
    )
