from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.routes import spu


class SpuDatabase:
    def query_one(self, sql, params=None):
        if sql == Q.SHELL_COUNTS:
            return {"iter": 1, "inno": 0, "strategy": 0, "search": 1, "revived": 0}
        assert sql == Q.SPU_DETAIL
        assert params == ("SPU-1",)
        return {
            "spu": "SPU-1", "product_names": ["测试产品"], "skus": [],
            "category": "S支架类", "grade": "A级", "launch_period": "2026Q1",
            "message_count": 10, "negative_evi_count": 3,
            "positive_evi_count": 7, "negative_ratio": 0.3, "avg_star": 4.1,
            "open_issue_count": 0, "issue_count": 0,
            "median_negative_ratio": 0.173, "median_avg_star": 4.27,
        }

    def query(self, sql, params=None):
        assert sql == Q.SPU_ISSUES
        assert params == ("SPU-1", "SPU-1")
        return []


def test_spu_page_renders_live_medians(monkeypatch):
    fake = SpuDatabase()
    monkeypatch.setattr(spu, "db", fake)
    monkeypatch.setattr(web, "db", fake)
    app = FastAPI()
    static_dir = Path(spu.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(spu.router)

    response = TestClient(app).get("/spu/SPU-1")
    assert response.status_code == 200
    assert "组内中位 17.3%" in response.text
    assert "组内中位 4.27" in response.text
