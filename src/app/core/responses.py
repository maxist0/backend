"""Builders for the success/error response envelope."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.schemas import (
    ErrorCode,
    ErrorDetail,
    ErrorResponse,
    ResponseMetadata,
    SuccessResponse,
)


def create_response_metadata(
    request: Request | None = None, start_time: datetime | None = None
) -> ResponseMetadata:
    """Create response metadata with optional request information."""
    metadata_dict: dict[str, Any] = {
        "request_id": str(uuid.uuid4()),
        "version": get_settings().APP_VERSION,
    }

    if start_time is not None:
        metadata_dict["execution_time"] = (datetime.now() - start_time).total_seconds()

    # Extract request ID from headers if available
    if request and hasattr(request, "headers"):
        request_id = request.headers.get("X-Request-ID")
        if request_id:
            metadata_dict["request_id"] = request_id

    return ResponseMetadata(**metadata_dict)


def success_response(
    data: dict[str, Any] | list[Any] | str | int | float | bool | None = None,
    request: Request | None = None,
    start_time: datetime | None = None,
    status_code: int = 200,
    top_level_fields: dict[str, Any] | None = None,
) -> JSONResponse:
    """Create a standardized success response."""
    metadata = create_response_metadata(request=request, start_time=start_time)
    response = SuccessResponse(data=data, metadata=metadata, success=True)
    response_content = response.model_dump()
    if top_level_fields:
        response_content.update(top_level_fields)
    return JSONResponse(content=response_content, status_code=status_code)


def error_response(
    message: str,
    error_code: ErrorCode = ErrorCode.INTERNAL_ERROR,
    field: str | None = None,
    details: dict[str, Any] | None = None,
    request: Request | None = None,
    start_time: datetime | None = None,
    status_code: int = 400,
) -> JSONResponse:
    """Create a standardized error response."""
    metadata = create_response_metadata(request=request, start_time=start_time)
    error_detail = ErrorDetail(
        code=error_code, message=message, field=field, details=details
    )
    response = ErrorResponse(
        error=error_detail, metadata=metadata, data=None, success=False
    )
    return JSONResponse(content=response.model_dump(), status_code=status_code)


# Convenience functions for common HTTP responses
def ok_response(
    data: Any | None = None,
    message: str | None = None,
    request: Request | None = None,
    start_time: datetime | None = None,
) -> JSONResponse:
    """Create a 200 OK response."""
    if message and not data:
        data = {"message": message}
    return success_response(
        data=data, request=request, start_time=start_time, status_code=200
    )


def created_response(
    data: Any | None = None,
    message: str = "Resource created successfully",
    request: Request | None = None,
    start_time: datetime | None = None,
) -> JSONResponse:
    """Create a 201 Created response."""
    if not data:
        data = {"message": message}
    return success_response(
        data=data, request=request, start_time=start_time, status_code=201
    )
