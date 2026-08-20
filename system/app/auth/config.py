"""登录、会话与授权配置。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit


class AuthConfigError(RuntimeError):
    """认证配置缺失或格式错误。"""


@dataclass(frozen=True, slots=True)
class AuthConfig:
    feishu_app_id: str
    feishu_app_secret: str
    session_secret: str
    public_base_urls: tuple[str, ...]
    allowed_open_ids: frozenset[str]
    writer_open_ids: frozenset[str]
    cookie_secure: bool
    # 灰度开关：开启后所有 GET 免登录可读，写入仍需登录 + 在可写名单。
    # 默认关闭——这是把内部商业情报暴露给公网的开关，必须显式打开才生效。
    anonymous_read: bool = False


_NONEMPTY_REQUIRED = (
    "VOC_FEISHU_APP_ID",
    "VOC_FEISHU_APP_SECRET",
    "VOC_SESSION_SECRET",
    "VOC_PUBLIC_BASE_URLS",
)
_PRESENT_REQUIRED = (
    "VOC_ALLOWED_OPEN_IDS",
    "VOC_WRITER_OPEN_IDS",
)


def _parse_base_urls(raw: str) -> tuple[str, ...]:
    urls: list[str] = []
    for item in raw.split(","):
        value = item.strip().rstrip("/")
        if not value:
            continue
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise AuthConfigError(
                "VOC_PUBLIC_BASE_URLS 只能包含不带路径的 http(s) 站点根地址："
                f"{value!r}"
            )
        if value not in urls:
            urls.append(value)
    if not urls:
        raise AuthConfigError("VOC_PUBLIC_BASE_URLS 至少需要一个站点根地址")
    return tuple(urls)


def _parse_open_ids(raw: str) -> frozenset[str]:
    return frozenset(value.strip() for value in raw.split(",") if value.strip())


def _parse_bool(name: str, raw: str) -> bool:
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise AuthConfigError(f"{name} 必须是 true 或 false")


def load_auth_config(environ: Mapping[str, str] | None = None) -> AuthConfig:
    """从环境变量读取完整认证配置，配错时立即失败。"""
    source = os.environ if environ is None else environ
    missing = [name for name in _NONEMPTY_REQUIRED if not source.get(name, "").strip()]
    missing.extend(name for name in _PRESENT_REQUIRED if name not in source)
    if missing:
        raise AuthConfigError("缺少认证环境变量：" + ", ".join(missing))

    cookie_secure_raw = source.get("VOC_COOKIE_SECURE", "").strip() or "true"
    cookie_secure = _parse_bool("VOC_COOKIE_SECURE", cookie_secure_raw)
    # 未设置即视为关闭：漏配不应该悄悄把看板暴露出去。
    anon_raw = source.get("VOC_ANONYMOUS_READ", "").strip() or "false"
    anonymous_read = _parse_bool("VOC_ANONYMOUS_READ", anon_raw)
    return AuthConfig(
        feishu_app_id=source["VOC_FEISHU_APP_ID"].strip(),
        feishu_app_secret=source["VOC_FEISHU_APP_SECRET"].strip(),
        session_secret=source["VOC_SESSION_SECRET"].strip(),
        public_base_urls=_parse_base_urls(source["VOC_PUBLIC_BASE_URLS"]),
        # 安全默认必须 fail-closed：空名单拒绝所有人，绝不解释为全员放行。
        allowed_open_ids=_parse_open_ids(source["VOC_ALLOWED_OPEN_IDS"]),
        writer_open_ids=_parse_open_ids(source["VOC_WRITER_OPEN_IDS"]),
        cookie_secure=cookie_secure,
        anonymous_read=anonymous_read,
    )
