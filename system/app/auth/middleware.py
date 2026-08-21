"""全局登录门、两级授权与 Origin CSRF 校验。"""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable
from urllib.parse import quote

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse, RedirectResponse, Response

from .config import AuthConfig
from .session import (
    SESSION_COOKIE,
    AuthenticatedUser,
    WriterPermissionRequired,
    decode_session,
    require_writer,
    session_needs_refresh,
    set_session_cookie,
)

_PUBLIC_PREFIXES = ("/auth/", "/static/")
_LOGIN_REQUIRED_READ_PATHS = frozenset({"/architecture"})
_WRITE_PATH = re.compile(
    r"^(?:/issue/[^/]+/[^/]+/status|/inno/[^/]+/status)$"
)


def _is_public_path(path: str) -> bool:
    return path == "/healthz" or path.startswith(_PUBLIC_PREFIXES)


def _login_target(path: str) -> str:
    return f"/auth/login?next={quote(path, safe='/')}"


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, *, config: AuthConfig, templates: Any):
        super().__init__(app)
        self.config = config
        self.templates = templates

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        user = self._read_user(request)
        path = request.url.path

        if _is_public_path(path):
            response = await call_next(request)
        elif (
            self.config.anonymous_read
            and request.method in ("GET", "HEAD")
            and path not in _LOGIN_REQUIRED_READ_PATHS
        ):
            # 灰度期：查看类请求免登录放行。
            # 写入是 POST，落不到这个分支，仍要走下面完整的
            # 会话 → 可读名单 → Origin → 可写名单 四道校验，
            # 所以 updated_by 依然只会是真人，审计链不受影响。
            response = await call_next(request)
        elif user is None:
            response = self._unauthenticated_response(request)
        elif user.open_id not in self.config.allowed_open_ids:
            # 空可读名单也必须拒绝所有人（fail-closed）。
            response = self.templates.TemplateResponse(
                request,
                "auth/denied.html",
                {"current_user": user},
                status_code=403,
            )
        elif request.method != "GET" and not self._valid_origin(request):
            response = PlainTextResponse(
                "请求来源不在 VOC_PUBLIC_BASE_URLS 白名单中，已拒绝本次操作。",
                status_code=403,
            )
        elif request.method == "POST" and _WRITE_PATH.fullmatch(path):
            try:
                require_writer(user, self.config)
            except WriterPermissionRequired as exc:
                response = PlainTextResponse(str(exc), status_code=403)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)

        if (
            user is not None
            and not path.startswith("/auth/")
            and session_needs_refresh(user)
        ):
            set_session_cookie(response, user, self.config)
        return response

    def _read_user(self, request: Request) -> AuthenticatedUser | None:
        token = request.cookies.get(SESSION_COOKIE)
        user = decode_session(token, self.config.session_secret) if token else None
        request.state.current_user = user
        return user

    def _valid_origin(self, request: Request) -> bool:
        origin = (request.headers.get("origin") or "").strip().rstrip("/")
        return origin in self.config.public_base_urls

    @staticmethod
    def _unauthenticated_response(request: Request) -> Response:
        login_url = _login_target(request.url.path)
        if request.headers.get("HX-Request", "").lower() == "true":
            return PlainTextResponse(
                "登录已失效，正在跳转到飞书登录。",
                status_code=401,
                headers={"HX-Redirect": login_url},
            )
        if request.method == "GET":
            return RedirectResponse(login_url, status_code=302)
        return PlainTextResponse(
            "请先用飞书登录后再提交；本次表单未被处理。",
            status_code=403,
        )
