# src/common/responses.py

from typing import Any, Dict, List, Optional, Union
from fastapi import Request
from fastapi.responses import JSONResponse
from datetime import datetime
import uuid
from .schemas import (
    SuccessResponse,
    ErrorResponse,
    PaginatedResponse,
    PaginationInfo,
    ErrorDetail,
    ErrorCode,
    ResponseMetadata,
)
from .config import settings


def create_response_metadata(
    request: Optional[Request] = None, start_time: Optional[datetime] = None
) -> ResponseMetadata:
    """Create response metadata with optional request information."""
    metadata_dict: dict[str, Any] = {
        "request_id": str(uuid.uuid4()),
        "version": settings.APP_VERSION,
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
    data: Optional[Union[Dict[str, Any], List[Any], str, int, float, bool]] = None,
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
    status_code: int = 200,
) -> JSONResponse:
    """Create a standardized success response."""
    metadata = create_response_metadata(request=request, start_time=start_time)
    response = SuccessResponse(data=data, metadata=metadata, success=True)
    return JSONResponse(content=response.model_dump(), status_code=status_code)


def error_response(
    message: str,
    error_code: ErrorCode = ErrorCode.INTERNAL_ERROR,
    field: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
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


def paginated_response(
    data: List[Any],
    total_records: int,
    current_page: int = 1,
    limit: int = 20,
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
    base_url: str = "/api",
) -> JSONResponse:
    """Create a standardized paginated response."""
    metadata = create_response_metadata(request=request, start_time=start_time)

    # Calculate pagination info
    total_pages = (total_records + limit - 1) // limit  # Ceiling division

    # Generate next/prev page URLs
    next_page = None
    prev_page = None

    if current_page < total_pages:
        next_page = f"{base_url}?page={current_page + 1}&limit={limit}"

    if current_page > 1:
        prev_page = f"{base_url}?page={current_page - 1}&limit={limit}"

    pagination_info = PaginationInfo(
        total_records=total_records,
        current_page=current_page,
        total_pages=total_pages,
        limit=limit,
        next_page=next_page,
        prev_page=prev_page,
    )

    response = PaginatedResponse(
        data=data,
        pagination=pagination_info,
        metadata=metadata,
        success=True,
        error=None,
    )
    return JSONResponse(content=response.model_dump(), status_code=200)


# Convenience functions for common HTTP responses
def ok_response(
    data: Optional[Any] = None,
    message: Optional[str] = None,
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 200 OK response."""
    if message and not data:
        data = {"message": message}
    return success_response(
        data=data, request=request, start_time=start_time, status_code=200
    )


def created_response(
    data: Optional[Any] = None,
    message: str = "Resource created successfully",
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 201 Created response."""
    if not data:
        data = {"message": message}
    return success_response(
        data=data, request=request, start_time=start_time, status_code=201
    )


def bad_request_response(
    message: str = "Bad Request",
    field: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 400 Bad Request response."""
    return error_response(
        error_code=ErrorCode.BAD_REQUEST,
        message=message,
        field=field,
        details=details,
        request=request,
        start_time=start_time,
        status_code=400,
    )


def unauthorized_response(
    message: str = "Unauthorized",
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 401 Unauthorized response."""
    return error_response(
        error_code=ErrorCode.UNAUTHORIZED,
        message=message,
        request=request,
        start_time=start_time,
        status_code=401,
    )


def forbidden_response(
    message: str = "Forbidden",
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 403 Forbidden response."""
    return error_response(
        error_code=ErrorCode.FORBIDDEN,
        message=message,
        request=request,
        start_time=start_time,
        status_code=403,
    )


def not_found_response(
    message: str = "Resource not found",
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 404 Not Found response."""
    return error_response(
        error_code=ErrorCode.NOT_FOUND,
        message=message,
        request=request,
        start_time=start_time,
        status_code=404,
    )


def conflict_response(
    message: str = "Resource conflict",
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 409 Conflict response."""
    return error_response(
        error_code=ErrorCode.CONFLICT,
        message=message,
        request=request,
        start_time=start_time,
        status_code=409,
    )


def rate_limited_response(
    message: str = "Rate limit exceeded",
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 429 Rate Limited response."""
    return error_response(
        error_code=ErrorCode.RATE_LIMITED,
        message=message,
        request=request,
        start_time=start_time,
        status_code=429,
    )


def internal_error_response(
    message: str = "Internal server error",
    request: Optional[Request] = None,
    start_time: Optional[datetime] = None,
) -> JSONResponse:
    """Create a 500 Internal Server Error response."""
    return error_response(
        error_code=ErrorCode.INTERNAL_ERROR,
        message=message,
        request=request,
        start_time=start_time,
        status_code=500,
    )
