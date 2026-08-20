from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import queries as Q
from app import web
from app.auth import feishu, session
from app.auth.config import AuthConfig, AuthConfigError, load_auth_config
from app.auth.middleware import AuthMiddleware
from app.auth.routes import origin_of, router as auth_router
from app.auth.session import AuthenticatedUser, encode_oauth_state, encode_session
from app.routes import board, home, innovation, issue, search, spu, strategy


READ_ID = "ou_reader_12345678"
WRITE_ID = "ou_writer_abcdef12"
PUBLIC_BASE = "http://testserver"
SESSION_SECRET = "test-session-secret"


class AuthDatabase:
    def __init__(self):
        self.executions: list[tuple[str, tuple]] = []

    def query_one(self, sql, params=None):
        if sql == Q.SHELL_COUNTS:
            return {"iter": 0, "inno": 0, "strategy": 0, "search": 0, "revived": 0}
        if sql in (Q.ISSUE_DETAIL, Q.INNOVATION_DETAIL):
            return {"status": "在跟进"}
        raise AssertionError("unexpected query_one")

    def query(self, sql, params=None):
        if sql in (Q.BOARD_SPUS, Q.BOARD_ISSUES):
            return []
        raise AssertionError("unexpected query")

    def execute(self, sql, params=None):
        self.executions.append((sql, tuple(params or ())))
        return 1


def make_config(
    *,
    allowed: frozenset[str] = frozenset({READ_ID, WRITE_ID}),
    writers: frozenset[str] = frozenset({WRITE_ID}),
    anonymous_read: bool = False,
) -> AuthConfig:
    return AuthConfig(
        feishu_app_id="cli_test",
        feishu_app_secret="app-secret",
        session_secret=SESSION_SECRET,
        public_base_urls=(PUBLIC_BASE,),
        allowed_open_ids=allowed,
        writer_open_ids=writers,
        cookie_secure=False,
        anonymous_read=anonymous_read,
    )


def make_client(monkeypatch, config: AuthConfig | None = None):
    fake = AuthDatabase()
    for module in (board, home, innovation, issue, search, spu, strategy, web):
        monkeypatch.setattr(module, "db", fake)

    app = FastAPI()
    app.state.auth_config = config or make_config()
    app.add_middleware(
        AuthMiddleware, config=app.state.auth_config, templates=web.templates
    )
    static_dir = Path(board.__file__).resolve().parents[1] / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.include_router(auth_router)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    for route in (
        home.router,
        board.router,
        spu.router,
        issue.router,
        innovation.router,
        strategy.router,
        search.router,
    ):
        app.include_router(route)
    return TestClient(app), fake


def set_login(
    client: TestClient,
    open_id: str,
    *,
    name: str = "张三",
    issued_at: int | None = None,
) -> None:
    when = int(time.time()) if issued_at is None else issued_at
    user = AuthenticatedUser(open_id=open_id, name=name, issued_at=when)
    client.cookies.set(
        session.SESSION_COOKIE,
        encode_session(user, SESSION_SECRET, issued_at=when),
    )


BUSINESS_GETS = (
    "/",
    "/iter",
    "/spu/SPU-1",
    "/issue/SPU-1/OPP-1",
    "/inno",
    "/inno/INNO-1",
    "/strategy",
    "/search",
)


@pytest.mark.parametrize("path", BUSINESS_GETS)
def test_all_business_gets_redirect_anonymous_users(monkeypatch, path):
    client, _ = make_client(monkeypatch)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == f"/auth/login?next={path}"


def test_htmx_anonymous_request_uses_hx_redirect(monkeypatch):
    client, _ = make_client(monkeypatch)
    response = client.get(
        "/iter", headers={"HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/auth/login?next=/iter"


@pytest.mark.parametrize(
    "path", ("/issue/SPU-1/OPP-1/status", "/inno/INNO-1/status")
)
def test_anonymous_status_post_is_forbidden_without_redirect(monkeypatch, path):
    client, _ = make_client(monkeypatch)
    response = client.post(path, data={"status": "在跟进"}, follow_redirects=False)
    assert response.status_code == 403
    assert "location" not in response.headers
    assert "本次表单未被处理" in response.text


@pytest.mark.parametrize("path", ("/healthz", "/static/app.css", "/auth/login"))
def test_public_routes_are_reachable_without_login(monkeypatch, path):
    client, _ = make_client(monkeypatch)
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 200


def test_login_start_sets_signed_ten_minute_state_cookie(monkeypatch):
    client, _ = make_client(monkeypatch)
    response = client.get(
        "/auth/login",
        params={"start": "true", "next": "/iter"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    target = urlsplit(response.headers["location"])
    query = parse_qs(target.query)
    assert target.path.endswith("/authen/v1/authorize")
    assert query["app_id"] == ["cli_test"]
    assert query["redirect_uri"] == [
        "http://testserver/auth/feishu/callback"
    ]
    assert len(query["state"][0]) >= 22
    state_cookie = response.headers["set-cookie"]
    assert "voc_oauth_state=" in state_cookie
    assert "Max-Age=600" in state_cookie
    assert "HttpOnly" in state_cookie
    assert "SameSite=lax" in state_cookie


def test_empty_allowed_list_is_fail_closed(monkeypatch):
    client, _ = make_client(monkeypatch, make_config(allowed=frozenset()))
    set_login(client, READ_ID)
    response = client.get("/iter")
    assert response.status_code == 403
    assert "请联系系统负责人把你的飞书账号加入名单" in response.text
    assert READ_ID in response.text


def test_reader_can_get_but_cannot_write_status(monkeypatch):
    client, fake = make_client(monkeypatch)
    set_login(client, READ_ID)
    assert client.get("/iter").status_code == 200
    for path in ("/issue/SPU-1/OPP-1/status", "/inno/INNO-1/status"):
        response = client.post(
            path,
            data={"status": "在跟进"},
            headers={"Origin": PUBLIC_BASE},
        )
        assert response.status_code == 403
        assert "只有查看权限" in response.text
    assert fake.executions == []


def test_writer_status_updates_include_real_actor(monkeypatch):
    client, fake = make_client(monkeypatch)
    set_login(client, WRITE_ID, name="李雷")
    for path in ("/issue/SPU-1/OPP-1/status", "/inno/INNO-1/status"):
        response = client.post(
            path,
            data={"status": "在跟进"},
            headers={"Origin": PUBLIC_BASE},
        )
        assert response.status_code == 200
    assert [params[-1] for _, params in fake.executions] == [
        "李雷(abcdef12)",
        "李雷(abcdef12)",
    ]


@pytest.mark.parametrize("case", ("missing", "mismatch", "expired"))
def test_callback_rejects_invalid_or_expired_state(monkeypatch, case):
    client, _ = make_client(monkeypatch)
    if case != "missing":
        client.cookies.set(
            session.OAUTH_STATE_COOKIE,
            encode_oauth_state("expected", "/iter", SESSION_SECRET),
        )
    if case == "expired":
        monkeypatch.setattr(session, "OAUTH_STATE_MAX_AGE", -1)
    state = None if case == "missing" else ("wrong" if case == "mismatch" else "expected")
    params = {"code": "code"}
    if state is not None:
        params["state"] = state
    response = client.get(
        "/auth/feishu/callback", params=params, follow_redirects=False
    )
    assert response.status_code == 400


@pytest.mark.parametrize("unsafe_next", ("https://evil.com", "//evil.com"))
def test_external_next_is_discarded(monkeypatch, unsafe_next):
    client, _ = make_client(monkeypatch)

    async def fake_exchange(config, code, redirect_uri):
        return "short-lived-token"

    async def fake_user(access_token):
        assert access_token == "short-lived-token"
        return {"open_id": READ_ID, "name": "张三", "avatar_url": ""}

    monkeypatch.setattr(feishu, "exchange_code", fake_exchange)
    monkeypatch.setattr(feishu, "fetch_user", fake_user)
    start = client.get(
        "/auth/login",
        params={"start": "true", "next": unsafe_next},
        follow_redirects=False,
    )
    assert start.status_code == 302
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = client.get(
        "/auth/feishu/callback",
        params={"state": state, "code": "authorization-code"},
        follow_redirects=False,
    )
    assert callback.status_code == 302
    assert callback.headers["location"] == "/"


def test_non_get_origin_must_be_whitelisted(monkeypatch):
    client, fake = make_client(monkeypatch)
    set_login(client, WRITE_ID)
    response = client.post(
        "/inno/INNO-1/status",
        data={"status": "在跟进"},
        headers={"Origin": "https://evil.com"},
    )
    assert response.status_code == 403
    assert "VOC_PUBLIC_BASE_URLS" in response.text
    assert fake.executions == []


def test_origin_of_uses_forwarded_headers_and_falls_back_for_spoofed_host():
    allowed = ("https://voc.ulanzi.com", "http://localhost:8000")
    assert origin_of(
        {
            "host": "127.0.0.1:8000",
            "x-forwarded-host": "voc.ulanzi.com",
            "x-forwarded-proto": "https, http",
        },
        allowed,
    ) == "https://voc.ulanzi.com"
    assert origin_of({"host": "evil.example"}, allowed) == allowed[0]


def test_missing_session_secret_fails_configuration_at_startup():
    environment = {
        "VOC_FEISHU_APP_ID": "cli_test",
        "VOC_FEISHU_APP_SECRET": "secret",
        "VOC_PUBLIC_BASE_URLS": "https://voc.ulanzi.com",
        "VOC_ALLOWED_OPEN_IDS": "",
        "VOC_WRITER_OPEN_IDS": "",
    }
    with pytest.raises(AuthConfigError, match="VOC_SESSION_SECRET"):
        load_auth_config(environment)


def test_empty_permission_variables_are_valid_but_deny_everyone():
    config = load_auth_config(
        {
            "VOC_FEISHU_APP_ID": "cli_test",
            "VOC_FEISHU_APP_SECRET": "secret",
            "VOC_SESSION_SECRET": SESSION_SECRET,
            "VOC_PUBLIC_BASE_URLS": "https://voc.ulanzi.com",
            "VOC_ALLOWED_OPEN_IDS": "",
            "VOC_WRITER_OPEN_IDS": "",
        }
    )
    assert config.allowed_open_ids == frozenset()
    assert config.writer_open_ids == frozenset()


def test_session_rolls_when_less_than_seven_days_remain(monkeypatch):
    client, _ = make_client(monkeypatch)
    old_issued_at = int(time.time()) - session.SESSION_REFRESH_AFTER - 1
    set_login(client, READ_ID, issued_at=old_issued_at)
    response = client.get("/iter")
    assert response.status_code == 200
    cookie = response.headers.get("set-cookie", "")
    assert "voc_session=" in cookie
    assert "Max-Age=1209600" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie


def test_every_write_route_is_covered_by_the_writer_gate():
    """写权限门是中间件里一条枚举式正则，漏掉的写路由会静默 fail-open。

    这条守卫不是重复验证现有两条写路径能不能挡住只读用户——那已经有专门的用例。
    它的意义在于：有人日后往任何一个业务 router 上加第三条写路由时，
    如果忘了同步 `_WRITE_PATH`，中间件不会报错，只会安静地让只读用户写进去。
    枚举式白名单的代价就得由一条会炸的测试来兜。
    """
    import re

    from app.auth.middleware import _WRITE_PATH

    uncovered: list[tuple[str, list[str]]] = []
    for module in (board, home, innovation, issue, search, spu, strategy):
        for route in module.router.routes:
            methods = set(getattr(route, "methods", set()) or set())
            non_get = methods - {"GET", "HEAD", "OPTIONS"}
            if not non_get:
                continue
            # 把 {spu} 这类占位符换成具体串再匹配，正则里用的是 [^/]+
            concrete = re.sub(r"\{[^}]+\}", "X", route.path)
            if not _WRITE_PATH.fullmatch(concrete):
                uncovered.append((route.path, sorted(non_get)))

    assert not uncovered, (
        "以下写路由不在 _WRITE_PATH 覆盖范围内，只读名单里的用户将能直接写入："
        f"{uncovered}；请同步修改 app/auth/middleware.py 的 _WRITE_PATH"
    )


# ── 灰度开关：匿名可读、登录可写 ──────────────────────────────────────────

def test_anonymous_read_defaults_off_so_a_missing_env_var_cannot_expose_the_board(
    monkeypatch,
):
    """漏配这个开关必须等于关闭。

    它控制的是「把内部商业情报放到公网上」，默认值站错边的代价不对称：
    默认关了最多是有人抱怨要登录，默认开了就是数据裸奔。
    """
    client, _ = make_client(monkeypatch)
    assert client.get("/iter", follow_redirects=False).status_code == 302


def test_anonymous_read_lets_viewers_in_without_a_session(monkeypatch):
    # 只探 /iter：本文件的假库夹具只备了老品迭代那两条查询，
    # 换别的页面会因为夹具而非鉴权失败。放行与否由中间件统一决定，
    # 一条路径足以证明匿名 GET 能过闸。
    client, _ = make_client(monkeypatch, make_config(anonymous_read=True))
    assert client.get("/iter", follow_redirects=False).status_code == 200


def test_anonymous_read_still_refuses_writes_without_a_session(monkeypatch):
    """灰度放开的只有「看」。

    写入仍要求登录 + 在可写名单，否则 updated_by 会退回常量，
    今天刚建立起来的审计链当场作废——这正是本开关刻意不碰的部分。
    """
    client, fake = make_client(monkeypatch, make_config(anonymous_read=True))
    resp = client.post(
        "/issue/SPU-1/OPP-1/status",
        data={"status": "在跟进"},
        headers={"Origin": PUBLIC_BASE},
        follow_redirects=False,
    )
    assert resp.status_code == 403
    assert fake.executions == []


def test_anonymous_htmx_write_is_bounced_to_login_rather_than_swapping_an_error(
    monkeypatch,
):
    """匿名访客点状态按钮时，htmx 要把他送去登录，而不是把 403 正文塞进局部容器。"""
    client, _ = make_client(monkeypatch, make_config(anonymous_read=True))
    resp = client.post(
        "/issue/SPU-1/OPP-1/status",
        data={"status": "在跟进"},
        headers={"Origin": PUBLIC_BASE, "HX-Request": "true"},
        follow_redirects=False,
    )
    assert resp.status_code == 401
    assert "/auth/login" in resp.headers.get("HX-Redirect", "")


def test_anonymous_read_does_not_leak_a_logged_out_user_into_updated_by(monkeypatch):
    """开着灰度开关时，写名单成员照常写入，操作人仍是真人。"""
    client, fake = make_client(monkeypatch, make_config(anonymous_read=True))
    set_login(client, WRITE_ID, name="张三")
    resp = client.post(
        "/issue/SPU-1/OPP-1/status",
        data={"status": "在跟进"},
        headers={"Origin": PUBLIC_BASE},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert fake.executions[-1][1][-1] == f"张三({WRITE_ID[-8:]})"
