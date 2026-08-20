"""VOC Web 认证对外入口。"""

from .config import AuthConfig, AuthConfigError, load_auth_config
from .middleware import AuthMiddleware
from .session import (
    AuthenticatedUser,
    actor_for_request,
    current_user,
    require_writer,
)

__all__ = [
    "AuthConfig",
    "AuthConfigError",
    "AuthMiddleware",
    "AuthenticatedUser",
    "actor_for_request",
    "current_user",
    "load_auth_config",
    "require_writer",
]
