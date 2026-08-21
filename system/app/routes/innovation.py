from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import queries as Q
from ..auth import actor_for_request
from ..db import DatabaseWriteError, db
from ..viewmodels import (attach_message_highlights, date_input,
                          display_status, status_mark)
from ..web import templates

router = APIRouter(prefix="/inno")


def _decorate(row: dict) -> dict:
    item = dict(row)
    item["brands"] = list(item.get("brands") or [])
    item["display_status"] = display_status(item.get("status"), item.get("revived_at"))
    item["mark_class"] = status_mark(item.get("status"), item.get("revived_at"))
    item["interactions"] = int(item.get("interactions") or 0)
    item["tagged_count"] = int(item.get("tagged_count") or 0)
    item["attached_count"] = int(item.get("attached_count") or 0)
    return item


@router.get("")
def innovation_pool(request: Request):
    innovations = [_decorate(row) for row in db.query(Q.BOARD_INNOVATIONS)]
    return templates.TemplateResponse(request, "inno-list.html", {
        "innovations": innovations,
    })


@router.get("/{opp_id}")
def innovation_detail(request: Request, opp_id: str):
    raw = db.query_one(Q.INNOVATION_DETAIL, (opp_id,))
    if not raw:
        raise HTTPException(404, "未找到该新品创新条目")
    opportunity = _decorate(raw)
    evidence = attach_message_highlights(
        db.query(Q.INNOVATION_EVIDENCE, (opp_id,)))
    return templates.TemplateResponse(request, "innovation-card.html", {
        "opportunity": opportunity,
        "evidence": evidence,
    })


@router.post("/{opp_id}/status", response_class=HTMLResponse)
async def update_innovation_status(request: Request, opp_id: str):
    existing = db.query_one(Q.INNOVATION_DETAIL, (opp_id,))
    if not existing:
        raise HTTPException(404, "未找到该新品创新条目")
    form = await request.form()
    status = str(form.get("status") or "")
    context = {
        "kind": "innovation",
        "opp_id": opp_id,
        "status": status or "考虑中",
        "decision_note": str(form.get("decision_note") or ""),
        "target_release": str(form.get("target_release") or ""),
        "release_date": str(form.get("release_date") or ""),
    }
    try:
        db.execute(Q.UPDATE_INNOVATION_STATUS, (
            opp_id,
            status,
            context["decision_note"],
            context["target_release"],
            date_input(context["release_date"]),
            actor_for_request(request),
        ))
    except (DatabaseWriteError, ValueError) as exc:
        context["error"] = str(exc)
        context["attempted_status"] = status
        try:
            persisted = db.query_one(Q.INNOVATION_DETAIL, (opp_id,))
        except AttributeError:
            persisted = None
        if persisted:
            context["status"] = persisted.get("status", "考虑中")
        return templates.TemplateResponse(
            request,
            "partials/status_control.html",
            context,
            status_code=422,
        )

    refreshed = db.query_one(Q.INNOVATION_DETAIL, (opp_id,)) or {"status": status}
    context.update({
        "status": refreshed.get("status", status),
        "target_release": refreshed.get("target_release"),
        "release_date": refreshed.get("release_date"),
        "decision_note": refreshed.get("decision_note"),
    })
    return templates.TemplateResponse(
        request, "partials/status_control.html", context
    )
