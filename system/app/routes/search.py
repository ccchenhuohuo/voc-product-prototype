from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..db import db  # 兼容独立路由测试的统一数据库桩；重定向本身不访问数据库。

router = APIRouter()

MAPPABLE_PARAMS = frozenset(("q", "category", "domain", "sub", "leaf", "sort", "dir"))


@router.get("/search")
def product_search_redirect(request: Request):
    params = {
        key: value
        for key, value in request.query_params.items()
        if key in MAPPABLE_PARAMS and value
    }
    params["filter"] = "all"
    return RedirectResponse(f"/iter?{urlencode(params)}", status_code=301)
