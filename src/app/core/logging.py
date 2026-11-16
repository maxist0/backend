"""
Structured logging via structlog.

Architecture
───────────────────────────────────────────────────────────────────────────
  Application code
      │  structlog.get_logger() / get_logger()
      ▼
  structlog processors  (add_log_level, timestamp, exc_info, …)
      │
      ▼  stdlib bridge  (ProcessorFormatter)
  stdlib logging.Handler(s)
      ├── StreamHandler(stdout)
      │       DEV  → structlog.dev.ConsoleRenderer  (coloured, human-readable)
      │       PROD → structlog.processors.JSONRenderer
      └── OTLPLogHandler  (when OTLP_LOGS_ENABLED=True)
                │  opentelemetry.sdk.logs.LoggingHandler
                ▼
          OTLPLogExporter  →  Grafana Alloy  (HTTP /v1/logs)
───────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import importlib.util
import logging
import socket
import sys
import warnings
from typing import TYPE_CHECKING, Any

import structlog


if TYPE_CHECKING:
    from app.core.config import Settings


def _parse_otlp_headers(raw: str) -> dict[str, str]:
    """Parse `KEY=VALUE,KEY2=VALUE2` header string into a dict."""
    headers: dict[str, str] = {}
    for part in raw.split(","):
        part = part.strip()
        if "=" in part:
            k, _, v = part.partition("=")
            headers[k.strip()] = v.strip()
    return headers


def _build_otlp_handler(settings: "Settings") -> logging.Handler | None:
    """
    Create a stdlib logging.Handler that exports to an OTLP endpoint.

    Returns *None* if the required packages are missing or configuration is
    invalid – so the app can still start without the OTel extras installed.
    """
    try:
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk._logs import LoggingHandler as OtelLoggingHandler
        from opentelemetry.exporter.otlp.proto.http._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry.sdk.resources import Resource, SERVICE_NAME
        from opentelemetry._logs import set_logger_provider
    except ImportError as exc:
        logging.getLogger(__name__).warning(
            "OTLP log export requested but opentelemetry packages are not "
            "installed – skipping.  Install: opentelemetry-sdk "
            "opentelemetry-exporter-otlp-proto-http  (%s)",
            exc,
        )
        return None

    headers = (
        _parse_otlp_headers(settings.OTLP_HEADERS) if settings.OTLP_HEADERS else {}
    )

    exporter = OTLPLogExporter(
        endpoint=f"{settings.OTLP_ENDPOINT.rstrip('/')}/v1/logs",
        headers=headers,
    )

    resource = Resource(attributes={SERVICE_NAME: settings.OTLP_SERVICE_NAME})
    provider = LoggerProvider(resource=resource)
    provider.add_log_record_processor(BatchLogRecordProcessor(exporter))
    set_logger_provider(provider)

    # opentelemetry-sdk >= 1.40 deprecates its LoggingHandler in favour of the
    # one in opentelemetry-instrumentation-logging (warns in __init__). That
    # replacement (0.66b2) exports structlog's event dict as a repr string
    # instead of a structured body, so the switch belongs to the observability
    # redesign. Until then only this one warning is silenced.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="`LoggingHandler` in `opentelemetry-sdk` is deprecated",
            category=DeprecationWarning,
        )
        otlp_handler = OtelLoggingHandler(logger_provider=provider)
    otlp_handler.setLevel(logging.DEBUG)
    return otlp_handler


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


def _make_static_fields_processor(environment: str) -> Any:
    """
    Return a structlog processor that stamps every record with:
      - `hostname`  – the machine name (resolved once at startup)
      - `environment` – dev / staging / prod (from Settings)
    Uses `setdefault` so callers can still override either field.
    """
    _hostname = socket.gethostname()
    _env = environment

    def _processor(_logger: Any, _method: str, event_dict: dict) -> dict:
        event_dict.setdefault("hostname", _hostname)
        event_dict.setdefault("environment", _env)
        return event_dict

    return _processor


_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.ExceptionRenderer(),
]


def setup_logging(settings: "Settings") -> None:
    """
    Configure structlog + stdlib logging from *settings*.

    Logs are written to stdout only.  When OTLP_LOGS_ENABLED is True,
    records are additionally pushed to an OTLP/HTTP endpoint (Grafana Alloy).

    Called once per process: by the lifespan (API) and by the taskiq
    `WORKER_STARTUP` hook (worker). Safe to call again (the test suite
    runs one lifespan per test): handlers are replaced, not added.
    """
    log_level_int = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)

    environment_str = (
        settings.ENVIRONMENT.value
        if hasattr(settings.ENVIRONMENT, "value")
        else str(settings.ENVIRONMENT)
    )
    static_fields = _make_static_fields_processor(environment_str)
    # Position 1: runs right after the contextvars are merged. Inserted once
    # per process, so repeated setup (hot reload, tests) adds no duplicate.
    if not any(getattr(p, "__name__", "") == "_processor" for p in _SHARED_PROCESSORS):
        _SHARED_PROCESSORS.insert(1, static_fields)

    json_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
        foreign_pre_chain=_SHARED_PROCESSORS,
    )

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=_console_colors()),
        ],
        foreign_pre_chain=_SHARED_PROCESSORS,
    )

    handlers: list[logging.Handler] = []

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level_int)
    stdout_handler.setFormatter(
        console_formatter if not settings.is_production else json_formatter
    )
    handlers.append(stdout_handler)

    if settings.OTLP_LOGS_ENABLED:
        otlp_handler = _build_otlp_handler(settings)
        if otlp_handler is not None:
            handlers.append(otlp_handler)

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level_int)
    root_logger.handlers = []  # avoid duplicates on hot-reload
    for h in handlers:
        root_logger.addHandler(h)

    # Silence noisy third-party loggers
    for name in (
        "uvicorn.access",
        "uvicorn.error",
        "sqlalchemy.engine",
        "urllib3",
        "sqlalchemy",
        "python_multipart.multipart",
    ):
        logger = logging.getLogger(name)
        logger.setLevel(logging.WARNING)
        logger.handlers = []  # Clear any existing handlers
        logger.propagate = False
    if settings.ACCESS_LOG_ENABLED:
        access_log = logging.getLogger("access")
        access_log.setLevel(logging.INFO)
        access_log.propagate = False  # don't double-write to root

        access_stdout = logging.StreamHandler(sys.stdout)
        access_stdout.setLevel(logging.INFO)
        access_stdout.setFormatter(
            console_formatter if not settings.is_production else json_formatter
        )
        access_log.handlers = []
        access_log.addHandler(access_stdout)

        if settings.OTLP_LOGS_ENABLED:
            for h in handlers:
                from opentelemetry.sdk._logs import LoggingHandler as _OtelH

                if isinstance(h, _OtelH):
                    access_log.addHandler(h)
                    break

    structlog.configure(
        processors=_SHARED_PROCESSORS
        + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Return a structlog logger bound to *name*.

    Usage::

        from app.core import get_logger
        logger = get_logger(__name__)
        logger.info("user_created", user_id=42)
    """
    return structlog.get_logger(name)


def get_access_logger() -> structlog.stdlib.BoundLogger:
    """Return the dedicated HTTP access logger."""
    return structlog.get_logger("access")
