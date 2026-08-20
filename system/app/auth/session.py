"""无状态签名 cookie 与当前用户辅助。"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from itsdangerous import BadData, SignatureExpired, URLSafeTimedSerializer
from starlette.responses import Response

from .config import AuthConfig

SESSION_COOKIE = "voc_session"
OAUTH_STATE_COOKIE = "voc_oauth_state"
SESSION_MAX_AGE = 14 * 24 * 60 * 60
SESSION_REFRESH_AFTER = 7 * 24 * 60 * 60
OAUTH_STATE_MAX_AGE = 10 * 60
_SESSION_SALT = "voc-session-v1"
_OAUTH_STATE_SALT = "voc-oauth-state-v1"


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    open_id: str
    name: str
    issued_at: int


class InvalidOAuthState(ValueError):
    """OAuth state cookie 缺失、签名无效或已过期。"""


class WriterPermissionRequired(PermissionError):
    """当前账号不在可写名单。"""


def _serializer(secret: str, salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key=secret, salt=salt)


def encode_session(
    user: AuthenticatedUser, secret: str, *, issued_at: int | None = None
) -> str:
    timestamp = int(time.time()) if issued_at is None else int(issued_at)
    # 会话 payload 只允许这三个字段；飞书 token 不得进入 cookie。
    payload = {"open_id": user.open_id, "name": user.name, "issued_at": timestamp}
    return _serializer(secret, _SESSION_SALT).dumps(payload)


def decode_session(token: str, secret: str) -> AuthenticatedUser | None:
    try:
        payload = _serializer(secret, _SESSION_SALT).loads(
            token, max_age=SESSION_MAX_AGE
        )
    except (BadData, SignatureExpired):
        return None
    if not isinstance(payload, dict) or set(payload) != {"open_id", "name", "issued_at"}:
        return None
    open_id = payload.get("open_id")
    name = payload.get("name")
    issued_at = payload.get("issued_at")
    if (
        not isinstance(open_id, str)
        or not open_id
        or not isinstance(name, str)
        or not name
        or not isinstance(issued_at, int)
    ):
        return None
    return AuthenticatedUser(open_id=open_id, name=name, issued_at=issued_at)


def session_needs_refresh(user: AuthenticatedUser, *, now: int | None = None) -> bool:
    current = int(time.time()) if now is None else int(now)
    return current - user.issued_at > SESSION_REFRESH_AFTER


def set_session_cookie(
    response: Response, user: AuthenticatedUser, config: AuthConfig
) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        encode_session(user, config.session_secret),
        max_age=SESSION_MAX_AGE,
        path="/",
        secure=config.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def delete_session_cookie(response: Response, config: AuthConfig) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        path="/",
        secure=config.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def encode_oauth_state(state: str, next_path: str, secret: str) -> str:
    return _serializer(secret, _OAUTH_STATE_SALT).dumps(
        {"state": state, "next": next_path}
    )


def decode_oauth_state(token: str | None, secret: str) -> dict[str, str]:
    if not token:
        raise InvalidOAuthState("OAuth state cookie 缺失")
    try:
        payload: Any = _serializer(secret, _OAUTH_STATE_SALT).loads(
            token, max_age=OAUTH_STATE_MAX_AGE
        )
    except SignatureExpired as exc:
        raise InvalidOAuthState("OAuth state 已过期") from exc
    except BadData as exc:
        raise InvalidOAuthState("OAuth state 签名无效") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"state", "next"}
        or not isinstance(payload.get("state"), str)
        or not isinstance(payload.get("next"), str)
    ):
        raise InvalidOAuthState("OAuth state 内容无效")
    return {"state": payload["state"], "next": payload["next"]}


def set_oauth_state_cookie(
    response: Response, state: str, next_path: str, config: AuthConfig
) -> None:
    response.set_cookie(
        OAUTH_STATE_COOKIE,
        encode_oauth_state(state, next_path, config.session_secret),
        max_age=OAUTH_STATE_MAX_AGE,
        path="/",
        secure=config.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def delete_oauth_state_cookie(response: Response, config: AuthConfig) -> None:
    response.delete_cookie(
        OAUTH_STATE_COOKIE,
        path="/",
        secure=config.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def current_user(request: Request) -> AuthenticatedUser | None:
    return getattr(request.state, "current_user", None)


def require_writer(
    user: AuthenticatedUser, config: AuthConfig
) -> AuthenticatedUser:
    if user.open_id not in config.writer_open_ids:
        raise WriterPermissionRequired(
            "你当前只有查看权限，无法修改状态。请联系系统负责人将你的飞书账号加入可写名单。"
        )
    return user


def actor_for_request(request: Request) -> str:
    user = current_user(request)
    if user is not None:
        return f"{user.name}({user.open_id[-8:]})"
    # 只有脱离全局认证中间件的降级路径（如轻量单测）才会走到这里。
    return os.environ.get("VOC_APP_USER", "voc_human")
