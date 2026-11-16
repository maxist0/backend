"""Public facade of the core.

Modules import infrastructure from here, not from the submodules:

    from app.core import DbSession, SettingsDep, get_logger, task

The core never imports from `app.modules`. Importing this package reads no
settings and opens no connection.
"""

from app.core.config import Settings, SettingsDep, get_settings
from app.core.database import Base, DbSession, utcnow
from app.core.errors import log_unexpected_error
from app.core.logging import get_logger
from app.core.module import Module
from app.core.tasks import task

__all__ = [
    "Base",
    "DbSession",
    "Module",
    "Settings",
    "SettingsDep",
    "get_logger",
    "get_settings",
    "log_unexpected_error",
    "task",
    "utcnow",
]
