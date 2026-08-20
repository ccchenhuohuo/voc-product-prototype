"""模板与通用 Web 辅助。"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlencode

from fastapi.templating import Jinja2Templates

from . import queries as Q
from .auth.session import current_user
from .db import db
from .viewmodels import (
    format_number,
    product_name,
)

APP_DIR = Path(__file__).resolve().parent


def auth_context(request) -> dict[str, object]:
    """所有完整页面从统一渲染入口取当前登录人。"""
    return {"current_user": current_user(request)}


templates = Jinja2Templates(
    directory=str(APP_DIR / "templates"),
    context_processors=[auth_context],
)


def shell_counts() -> dict[str, int]:
    """侧栏读数只发一个查询；在完整页面渲染时由 base.html 调用。"""
    row = db.query_one(Q.SHELL_COUNTS) or {}
    return {
        key: int(row.get(key) or 0)
        for key in ("products", "iter", "inno", "strategy", "revived")
    }


def query_url(request, **overrides: object) -> str:
    """保留当前 GET 筛选，只替换指定维度；空值表示清除。"""
    params = dict(request.query_params)
    for key, value in overrides.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    query = urlencode(params)
    return request.url.path + (f"?{query}" if query else "")


templates.env.globals.update(
    product_name=product_name,
    shell_counts=shell_counts,
    query_url=query_url,
)
templates.env.filters["num"] = format_number
