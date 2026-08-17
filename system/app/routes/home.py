from __future__ import annotations

from fastapi import APIRouter, Request

from .. import queries as Q
from ..db import db
from ..viewmodels import normalize_home_dashboard
from ..web import templates

router = APIRouter()


@router.get("/")
def home(request: Request):
    dashboard = normalize_home_dashboard(
        funnel_rows=db.query(Q.HOME_EVIDENCE_FUNNEL),
        evidence_rows=db.query(Q.HOME_EVIDENCE_PER_OPP),
        similarity_rows=db.query(Q.HOME_SIMILARITY),
        status_rows=db.query(Q.HOME_ISSUE_STATUS),
        coverage_rows=db.query(Q.HOME_COVERAGE),
        freshness_rows=db.query(Q.HOME_FRESHNESS),
    )
    return templates.TemplateResponse(request, "home.html", dashboard)
