from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from .. import queries as Q
from ..db import DatabaseWriteError, db
from ..viewmodels import date_input, decorate_issue, voice_summary
from ..web import templates

router = APIRouter(prefix="/issue")


@router.get("/{spu}/{opp_id}")
def issue_voices(request: Request, spu: str, opp_id: str):
    issue = db.query_one(Q.ISSUE_DETAIL, (spu, opp_id))
    if not issue:
        raise HTTPException(404, "未找到该问题条目")
    voices = db.query(Q.ISSUE_VOICES, (opp_id, spu))
    return templates.TemplateResponse(request, "issue-voices.html", {
        "issue": decorate_issue(issue),
        "voices": voices,
        "summary": voice_summary(voices),
    })


@router.post("/{spu}/{opp_id}/status", response_class=HTMLResponse)
async def update_issue_status(request: Request, spu: str, opp_id: str):
    existing = db.query_one(Q.ISSUE_DETAIL, (spu, opp_id))
    if not existing:
        raise HTTPException(404, "未找到该问题条目")
    form = await request.form()
    status = str(form.get("status") or "")
    context = {
        "kind": "issue",
        "spu": spu,
        "opp_id": opp_id,
        "status": status or "考虑中",
        "decision_note": str(form.get("decision_note") or ""),
        "target_release": str(form.get("target_release") or ""),
        "release_date": str(form.get("release_date") or ""),
    }
    try:
        db.execute(Q.UPDATE_ISSUE_STATUS, (
            spu, opp_id, status, context["decision_note"],
            context["target_release"], date_input(context["release_date"]),
            os.environ.get("VOC_APP_USER", "voc_human"),
        ))
    except (DatabaseWriteError, ValueError) as exc:
        context["error"] = str(exc)
        context["attempted_status"] = status
        try:
            persisted = db.query_one(Q.ISSUE_DETAIL, (spu, opp_id))
        except AttributeError:  # 只实现 execute 的轻量测试替身
            persisted = None
        if persisted:
            context.update({
                "status": persisted.get("status", "考虑中"),
                "revived_at": persisted.get("revived_at"),
            })
        response = templates.TemplateResponse(
            request, "partials/status_control.html", context, status_code=422
        )
        return response
    refreshed = db.query_one(Q.ISSUE_DETAIL, (spu, opp_id)) or {"status": status}
    context.update({
        "status": refreshed.get("status", status),
        "revived_at": (refreshed.get("revived_at")
                       if refreshed.get("status", status) == "考虑中" else None),
        "target_release": refreshed.get("target_release"),
        "release_date": refreshed.get("release_date"),
        "decision_note": refreshed.get("decision_note"),
    })
    response = templates.TemplateResponse(
        request, "partials/status_control.html", context
    )
    # 状态会同时改变 SPU 待办读数与侧栏待复议数；成功后让
    # HTMX 整页重取，确保三处都以同一个数据库快照渲染。
    response.headers["HX-Refresh"] = "true"
    return response
