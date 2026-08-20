"""飞书登录、回调、退出与无权页。"""
from __future__ import annotations

import secrets
import time
from typing import Mapping
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from ..web import templates
from . import feishu
from .config import AuthConfig
from .session import (
    OAUTH_STATE_COOKIE,
    AuthenticatedUser,
    InvalidOAuthState,
    current_user,
    decode_oauth_state,
    delete_oauth_state_cookie,
    delete_session_cookie,
    set_oauth_state_cookie,
    set_session_cookie,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def safe_next(value: str | None) -> str:
    """只保留站内绝对路径，防止 OAuth 回调成为开放重定向。"""
    if (
        not value
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        return "/"
    return value


def origin_of(headers: Mapping[str, str], public_base_urls: tuple[str, ...]) -> str:
    """按受信任反代头推导本次回调 origin，且必须命中白名单。"""
    host = (headers.get("x-forwarded-host") or headers.get("host") or "").strip()
    forwarded_proto = (headers.get("x-forwarded-proto") or "").split(",", 1)[0].strip()
    if forwarded_proto:
        proto = forwarded_proto
    else:
        local_host = host.lower().startswith(("localhost", "127."))
        proto = "http" if local_host else "https"
    guess = f"{proto}://{host}"
    return guess if guess in public_base_urls else public_base_urls[0]


def _config(request: Request) -> AuthConfig:
    return request.app.state.auth_config


def _callback_uri(request: Request, config: AuthConfig) -> str:
    return f"{origin_of(request.headers, config.public_base_urls)}/auth/feishu/callback"


@router.get("/login", name="auth_login")
def login(request: Request, next: str | None = None, start: bool = False):
    config = _config(request)
    next_path = safe_next(next)
    if not start:
        query = urlencode({"start": "true", "next": next_path})
        return templates.TemplateResponse(
            request,
            "auth/login.html",
            {"login_url": f"/auth/login?{query}"},
        )

    # token_urlsafe(16) 的随机原料正好是 128 位。
    state = secrets.token_urlsafe(16)
    redirect_uri = _callback_uri(request, config)
    response = RedirectResponse(
        feishu.authorization_url(config, redirect_uri, state), status_code=302
    )
    set_oauth_state_cookie(response, state, next_path, config)
    return response


def _state_error(message: str, config: AuthConfig):
    response = PlainTextResponse(message, status_code=400)
    delete_oauth_state_cookie(response, config)
    return response


@router.get("/feishu/callback", name="auth_feishu_callback")
async def feishu_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
):
    config = _config(request)
    try:
        state_data = decode_oauth_state(
            request.cookies.get(OAUTH_STATE_COOKIE), config.session_secret
        )
    except InvalidOAuthState as exc:
        return _state_error(str(exc), config)
    if state is None or not secrets.compare_digest(state, state_data["state"]):
        return _state_error("OAuth state 缺失或不匹配", config)
    if not code:
        return _state_error("飞书回调缺少 authorization code", config)

    redirect_uri = _callback_uri(request, config)
    access_token = await feishu.exchange_code(config, code, redirect_uri)
    try:
        user_data = await feishu.fetch_user(access_token)
    finally:
        # access_token 只用于紧接着的 user_info 请求，随后立即丢弃。
        del access_token

    user = AuthenticatedUser(
        open_id=user_data["open_id"],
        name=user_data["name"],
        issued_at=int(time.time()),
    )
    request.state.current_user = user
    if user.open_id not in config.allowed_open_ids:
        response = templates.TemplateResponse(
            request,
            "auth/denied.html",
            {"current_user": user},
            status_code=403,
        )
    else:
        response = RedirectResponse(safe_next(state_data["next"]), status_code=302)
    set_session_cookie(response, user, config)
    delete_oauth_state_cookie(response, config)
    return response


@router.get("/logout", name="auth_logout")
def logout(request: Request):
    config = _config(request)
    response = RedirectResponse("/auth/login", status_code=302)
    delete_session_cookie(response, config)
    delete_oauth_state_cookie(response, config)
    return response


@router.get("/denied", name="auth_denied")
def denied(request: Request):
    user = current_user(request)
    if user is None:
        return RedirectResponse("/auth/login?next=/", status_code=302)
    return templates.TemplateResponse(
        request,
        "auth/denied.html",
        {"current_user": user},
        status_code=403,
    )
