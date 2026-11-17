"""Structured logging with structlog, written to stdout.

`setup_logging` configures structlog and the stdlib root logger, so records
from structlog and from stdlib loggers (uvicorn, SQLAlchemy) share one
handler: the console renderer in `dev`, one JSON object per line otherwise.
A log collector reads the container output; the app exports nothing itself.
"""

import importlib.util
import logging
import socket
import sys

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

from app.core.config import Settings


def _console_colors() -> bool:
    """Colour the console renderer only on a TTY that can show colours.

    Container logs (no TTY) stay free of ANSI codes. On Windows, structlog
    needs colorama for colours and raises without it, so colours are only
    enabled when colorama is importable.
    """
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        return importlib.util.find_spec("colorama") is not None
    return True


def _static_fields(environment: str) -> Processor:
    """Return a processor that stamps `hostname` and `environment` on a record."""
    hostname = socket.gethostname()

    def add_fields(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("hostname", hostname)
        event_dict.setdefault("environment", environment)
        return event_dict

    return add_fields


def setup_logging(settings: Settings) -> None:
    """Configure structlog and stdlib logging from `settings`.

    Called once per process: by the lifespan (API) and by the taskiq
    `WORKER_STARTUP` hook (worker). A second call replaces the handler, so
    repeated lifespans (tests, reload) neither duplicate output nor keep the
    settings of the first call.
    """
    level = logging.getLevelNamesMapping().get(settings.LOG_LEVEL.upper(), logging.INFO)
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.ExceptionRenderer(),
    ]
    renderer: Processor = (
        structlog.dev.ConsoleRenderer(colors=_console_colors())
        if settings.is_development
        else structlog.processors.JSONRenderer()
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                _static_fields(settings.ENVIRONMENT.value),
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn's own handlers (from its default log config) are dropped so its
    # records, startup errors included, reach the root handler once. A
    # fileConfig() elsewhere in the process (Alembic's env.py) disables every
    # logger that existed before it ran. The request middleware's
    # `http.access` event replaces uvicorn's access log.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
        uvicorn_logger.disabled = False
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for name in ("sqlalchemy", "python_multipart"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Everything that depends on `settings` lives in the handler's formatter.
    # Loggers cached on first use keep this chain, which is the same on
    # every call, so a later call still takes effect.
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger named `name`.

    Usage:
        logger = get_logger(__name__)
        logger.info("users.created", user_id=str(user.id))
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
