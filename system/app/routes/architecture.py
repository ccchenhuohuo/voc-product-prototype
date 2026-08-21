"""数据架构说明页。

正文是随代码版本化的静态项目地图，不在路由中查询业务数据；共享页面壳层仍会
读取导航计数。内容以当前 Python、SQL 001–036、模板与离线契约为事实来源，
并显式区分仓库实现、默认关闭、环境待核验、产品规划和历史已替代。
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from ..web import templates

router = APIRouter(prefix="/architecture")


@router.get("")
def architecture(request: Request):
    return templates.TemplateResponse(request, "architecture.html", {})
