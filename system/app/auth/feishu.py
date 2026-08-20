"""飞书 OAuth 的三个端点客户端。"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

from .config import AuthConfig

FEISHU_BASE = "https://open.feishu.cn/open-apis"
REQUEST_TIMEOUT = 10.0


class FeishuProtocolError(RuntimeError):
    """飞书成功 HTTP 响应缺少契约字段。"""


def authorization_url(config: AuthConfig, redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "app_id": config.feishu_app_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{FEISHU_BASE}/authen/v1/authorize?{query}"


async def exchange_code(
    config: AuthConfig, code: str, redirect_uri: str
) -> str:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(
            f"{FEISHU_BASE}/authen/v2/oauth/token",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "grant_type": "authorization_code",
                "client_id": config.feishu_app_id,
                "client_secret": config.feishu_app_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
    # 保留 httpx/飞书原始错误，不改写成没有诊断信息的“登录失败”。
    response.raise_for_status()
    payload: Any = response.json()
    access_token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(access_token, str) or not access_token:
        raise FeishuProtocolError(f"飞书换令牌响应缺少 access_token：{payload!r}")
    return access_token


async def fetch_user(access_token: str) -> dict[str, str]:
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.get(
            f"{FEISHU_BASE}/authen/v1/user_info",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    response.raise_for_status()
    payload: Any = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    open_id = data.get("open_id") if isinstance(data, dict) else None
    name = data.get("name") if isinstance(data, dict) else None
    if not isinstance(open_id, str) or not open_id or not isinstance(name, str) or not name:
        raise FeishuProtocolError(f"飞书用户响应缺少 open_id/name：{payload!r}")
    return {
        "open_id": open_id,
        "name": name,
        "avatar_url": str(data.get("avatar_url") or ""),
    }
