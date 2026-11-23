"""Public facade of the core.

Modules import infrastructure from here, not from the submodules:

    from app.core import DbSession, SettingsDep, get_logger, task

The core never imports from `app.modules`. Importing this package reads no
settings and opens no connection.
"""

from app.core.admin import back_to_list, selected_ids, session_factory
from app.core.cache import RedisDep, cached, invalidate
from app.core.config import Settings, SettingsDep, get_settings
from app.core.database import (
    Base,
    CreatedAt,
    DbSession,
    Timestamps,
    UUIDPrimaryKey,
    utcnow,
    value_enum,
)
from app.core.errors import (
    AppError,
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    ProblemDetail,
    RateLimitedError,
    Unauthorized,
    constraint_name,
    log_unexpected_error,
    problem_responses,
)
from app.core.logging import get_logger
from app.core.mail import get_mailer, mail_delivery_error, send_templated_email
from app.core.module import Module
from app.core.rate_limiting import by_ip, by_ipv6_48, hit, rate_limit
from app.core.security import (
    TokenData,
    create_access_token,
    decode_token,
    equalize_password_timing,
    hash_password,
    hash_token,
    login_admission_limiter,
    password_needs_rehash,
    verify_password,
    wait_like_password_verify,
)
from app.core.tasks import TaskDbSession, TransientTaskError, task

__all__ = [
    "AppError",
    "BadRequest",
    "Base",
    "Conflict",
    "CreatedAt",
    "DbSession",
    "Forbidden",
    "Module",
    "NotFound",
    "ProblemDetail",
    "RateLimitedError",
    "RedisDep",
    "Settings",
    "SettingsDep",
    "TaskDbSession",
    "Timestamps",
    "TokenData",
    "TransientTaskError",
    "UUIDPrimaryKey",
    "Unauthorized",
    "back_to_list",
    "by_ip",
    "by_ipv6_48",
    "cached",
    "constraint_name",
    "create_access_token",
    "decode_token",
    "equalize_password_timing",
    "get_logger",
    "get_mailer",
    "get_settings",
    "hash_password",
    "hash_token",
    "hit",
    "invalidate",
    "log_unexpected_error",
    "login_admission_limiter",
    "mail_delivery_error",
    "password_needs_rehash",
    "problem_responses",
    "rate_limit",
    "selected_ids",
    "send_templated_email",
    "session_factory",
    "task",
    "utcnow",
    "value_enum",
    "verify_password",
    "wait_like_password_verify",
]
