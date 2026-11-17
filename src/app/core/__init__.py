"""Public facade of the core.

Modules import infrastructure from here, not from the submodules:

    from app.core import DbSession, SettingsDep, get_logger, task

The core never imports from `app.modules`. Importing this package reads no
settings and opens no connection.
"""

from app.core.cache import RedisDep, cached, invalidate
from app.core.config import Settings, SettingsDep, get_settings
from app.core.database import Base, DbSession, utcnow
from app.core.errors import (
    AppError,
    BadRequest,
    Conflict,
    Forbidden,
    NotFound,
    ProblemDetail,
    Unauthorized,
    log_unexpected_error,
    problem_responses,
)
from app.core.logging import get_logger
from app.core.module import Module
from app.core.tasks import TransientTaskError, task

__all__ = [
    "AppError",
    "BadRequest",
    "Base",
    "Conflict",
    "DbSession",
    "Forbidden",
    "Module",
    "NotFound",
    "ProblemDetail",
    "RedisDep",
    "Settings",
    "SettingsDep",
    "TransientTaskError",
    "Unauthorized",
    "cached",
    "get_logger",
    "get_settings",
    "invalidate",
    "log_unexpected_error",
    "problem_responses",
    "task",
    "utcnow",
]
