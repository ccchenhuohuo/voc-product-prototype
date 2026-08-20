"""数据架构说明页。

纯静态说明页：不查库、不依赖任何管道产出。口径与定义集中在这里，
其余页面因此不必各自铺解释性散文（见全局 UI 约定）。

内容对应 pipeline/docs/架构规格_v3.md，只呈现最新架构，不含版本对比。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..web import templates

router = APIRouter(prefix="/architecture")


@router.get("")
def architecture(request: Request):
    return templates.TemplateResponse(request, "architecture.html", {})
