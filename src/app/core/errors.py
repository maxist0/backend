"""Errors: exception types, envelope handlers and database error inspection.

The handlers still render the response envelope; RFC 9457 replaces them.

PostgreSQL error text can contain row values (`DETAIL: Key (email)=(...)`),
which are personal data. Code that reacts to a database error or logs one
uses only the structured fields read here: the exception type, the SQLSTATE
and the constraint name. They come from psycopg 3's diagnostics
(`exc.orig.diag`), the only supported driver.
"""

from datetime import datetime

import structlog
from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError

from app.core.logging import get_logger
from app.core.responses import error_response
from app.core.schemas import ErrorCode

logger = get_logger(__name__)


class AuthenticationError(HTTPException):
    """Missing or invalid credentials: 401 with `WWW-Authenticate: Bearer`."""

    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


def _driver_error(exc: DBAPIError) -> object:
    return exc.orig


def constraint_name(exc: DBAPIError) -> str | None:
    """Name of the violated constraint, or None if the driver did not report one."""
    # psycopg 3: Diagnostic object on the exception.
    diag = getattr(_driver_error(exc), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) and name else None


def sqlstate(exc: DBAPIError) -> str | None:
    """SQLSTATE code of the error (e.g. `23505` unique violation)."""
    code = getattr(_driver_error(exc), "sqlstate", None)
    return code if isinstance(code, str) and code else None


def find_db_error(exc: BaseException) -> DBAPIError | None:
    """Return the first database error in *exc*'s cause/context chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, DBAPIError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def log_unexpected_error(
    log: structlog.stdlib.BoundLogger, event: str, exc: BaseException
) -> None:
    """Log an unexpected exception at error level without its message text.

    Exception messages can carry personal data (a PostgreSQL `DETAIL` line
    echoes the conflicting email or username), so only `error_type` is
    logged. For database errors the SQLSTATE and constraint name are added
    and the traceback is left out, because rendering it would print the
    driver's message again. Other errors keep the traceback.
    """
    db_error = find_db_error(exc)
    if db_error is None:
        log.error(event, error_type=type(exc).__name__, exc_info=True)
        return
    log.error(
        event,
        error_type=type(exc).__name__,
        db_error_type=type(db_error).__name__,
        sqlstate=sqlstate(db_error),
        constraint=constraint_name(db_error),
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Answer any unhandled exception with a generic 500 envelope.

    Only the exception type is logged (see `log_unexpected_error`); the
    response carries no internals.
    """
    log_unexpected_error(logger, "app.unhandled_exception", exc)
    start_time = datetime.now()
    return error_response(
        message="An unexpected error occurred",
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code=ErrorCode.INTERNAL_ERROR,
        request=request,
        start_time=start_time,
    )


async def validation_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Return request-validation errors in the standard error envelope."""

    if not isinstance(exc, RequestValidationError):
        logger.warning(
            "app.validation_exception_handler_called_with_non_validation_error",
            error_type=type(exc).__name__,
        )
        return await generic_exception_handler(request, exc)

    start_time = datetime.now()
    errors = exc.errors()

    # Build a human-readable summary from the first error
    first = errors[0] if errors else {}
    loc = " -> ".join(str(loc) for loc in first.get("loc", []) if loc != "body")
    msg = first.get("msg", "Validation error")
    message = f"{loc}: {msg}" if loc else msg

    # Include all individual errors in details
    detail_list = [
        {
            "field": " -> ".join(str(loc) for loc in e.get("loc", []) if loc != "body"),
            "message": e.get("msg", ""),
            "type": e.get("type", ""),
        }
        for e in errors
    ]

    return error_response(
        message=message,
        error_code=ErrorCode.VALIDATION_ERROR,
        field=loc or None,
        details={"validation_errors": detail_list},
        request=request,
        start_time=start_time,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
    )


async def authentication_exception_handler(
    request: Request, exc: Exception
) -> JSONResponse:
    """Return authentication errors in the standard error envelope."""
    if not isinstance(exc, AuthenticationError):
        logger.warning(
            "app.authentication_exception_handler_called_with_non_authentication_error",
            error_type=type(exc).__name__,
        )
        return await generic_exception_handler(request, exc)

    start_time = datetime.now()
    detail = exc.detail
    return error_response(
        message="Authentication failed",
        status_code=status.HTTP_401_UNAUTHORIZED,
        error_code=ErrorCode.UNAUTHORIZED,
        details={"error": detail},
        request=request,
        start_time=start_time,
    )
