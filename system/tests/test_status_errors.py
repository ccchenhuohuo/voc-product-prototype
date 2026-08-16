from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db import DatabaseWriteError
from app.routes import innovation, issue


class RejectingDatabase:
    def query_one(self, *_args, **_kwargs):
        return {"status": "考虑中"}

    def execute(self, *_args, **_kwargs):
        raise DatabaseWriteError("置为「不考虑」时 decision_note 必填（证据基准与后续复议依赖它）")


class MissingDatabase:
    def query_one(self, *_args, **_kwargs):
        return None

    def execute(self, *_args, **_kwargs):
        raise AssertionError("目标不存在时不应写入")


class AcceptingDatabase:
    def query_one(self, *_args, **_kwargs):
        return {"status": "在跟进"}

    def execute(self, *_args, **_kwargs):
        return 1


def test_database_rejection_is_rendered_as_readable_prompt(monkeypatch):
    monkeypatch.setattr(issue, "db", RejectingDatabase())
    app = FastAPI()
    app.include_router(issue.router)
    response = TestClient(app).post("/issue/SPU-1/OPP-1/status", data={"status": "不考虑"})
    assert response.status_code == 422
    assert "decision_note 必填" in response.text


def test_issue_status_rejects_unknown_spu_opportunity_pair(monkeypatch):
    monkeypatch.setattr(issue, "db", MissingDatabase())
    app = FastAPI()
    app.include_router(issue.router)
    response = TestClient(app).post(
        "/issue/UNKNOWN/OPP-1/status", data={"status": "在跟进"}
    )
    assert response.status_code == 404


def test_innovation_status_rejects_non_innovation_target(monkeypatch):
    monkeypatch.setattr(innovation, "db", MissingDatabase())
    app = FastAPI()
    app.include_router(innovation.router)
    response = TestClient(app).post(
        "/inno/OLD-ISSUE/status", data={"status": "在跟进"}
    )
    assert response.status_code == 404


def test_issue_status_requests_full_refresh_after_success(monkeypatch):
    monkeypatch.setattr(issue, "db", AcceptingDatabase())
    app = FastAPI()
    app.include_router(issue.router)
    response = TestClient(app).post(
        "/issue/SPU-1/OPP-1/status",
        data={"status": "在跟进"},
        headers={"HX-Request": "true"},
    )
    assert response.status_code == 200
    assert response.headers["HX-Refresh"] == "true"
