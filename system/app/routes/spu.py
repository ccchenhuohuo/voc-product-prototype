from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from .. import queries as Q
from ..db import db
from ..viewmodels import sort_issues, sort_spus
from ..web import templates

router = APIRouter(prefix="/spu")


def _issues(spu: str) -> list[dict]:
    return sort_issues(db.query(Q.SPU_ISSUES, (spu, spu)))


def _max_evidence(issues: list[dict]) -> int:
    return max((int(row.get("evi_count") or 0) for row in issues), default=0)


@router.get("/{spu}")
def spu_detail(request: Request, spu: str):
    raw = db.query_one(Q.SPU_DETAIL, (spu,))
    if not raw:
        raise HTTPException(404, "未找到该 SPU")
    card = sort_spus([raw])[0]
    issues = _issues(spu)
    raw_voices = db.query(Q.SPU_RAW_VOICES, (spu,))
    return templates.TemplateResponse(request, "spu-card.html", {
        "card": card,
        "issues": issues,
        "raw_voices": raw_voices,
        "max_evidence": _max_evidence(issues),
    })


@router.get("/{spu}/issues")
def issue_list_fragment(request: Request, spu: str):
    issues = _issues(spu)
    return templates.TemplateResponse(request, "partials/issue_list.html", {
        "spu": spu,
        "issues": issues,
        "max_evidence": _max_evidence(issues),
    })
