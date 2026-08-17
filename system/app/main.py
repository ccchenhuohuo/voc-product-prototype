"""VOC 机会看板的 FastAPI ASGI 入口。

用途：组装路由与静态资源，并在应用生命周期内管理 PostgreSQL 连接池。
用法：在 ``system/`` 目录执行
      ``uvicorn app.main:app --host 127.0.0.1 --port 8000``。
前置条件：Python >= 3.11 且已安装本项目依赖；已在环境或 ``system/.env``
          中配置 ``VOC_PG_HOST``、``VOC_PG_PORT``、``VOC_PG_DB``、
          ``VOC_PG_HUMAN_USER`` 和 ``VOC_PG_HUMAN_PASSWORD``，且 PostgreSQL 可连通。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .db import db
from .routes import board, home, innovation, issue, search, spu, strategy

SYSTEM_DIR = Path(__file__).resolve().parents[1]
load_dotenv(SYSTEM_DIR / ".env")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.open()
    try:
        yield
    finally:
        db.close()


app = FastAPI(title="VOC 机会看板", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(home.router)
app.include_router(board.router)
app.include_router(spu.router)
app.include_router(issue.router)
app.include_router(innovation.router)
app.include_router(strategy.router)
app.include_router(search.router)
