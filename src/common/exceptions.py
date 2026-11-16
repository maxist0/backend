# src/common/exceptions.py
from datetime import datetime
from fastapi import Request, status
from fastapi.responses import JSONResponse

from src.common.responses import error_response
from src.common.schemas import ErrorCode
from src.api.auth.exceptions import AuthenticationError
from fastapi.exceptions import RequestValidationError
from src.common.logging import get_logger, log_unexpected_error


logger = get_logger(__name__)


## Validation Error Handler
"""
Override FastAPI's default validation error response to use the unified error_response format.
This will catch all Pydantic validation errors across the app and return them in a consistent structure.
"""


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all exception handler to return standardized error responses."""
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
    """Return Pydantic / request-validation errors in the standard error envelope."""

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
