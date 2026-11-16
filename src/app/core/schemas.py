"""Pydantic models of the success/error response envelope."""

from collections.abc import Callable
from datetime import datetime
from enum import Enum
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field


class ErrorCode(str, Enum):
    """Standard error codes for the API."""

    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    BAD_REQUEST = "BAD_REQUEST"
    CONFLICT = "CONFLICT"
    RATE_LIMITED = "RATE_LIMITED"


class ErrorDetail(BaseModel):
    """Detailed error information."""

    code: ErrorCode = Field(..., description="Error code")
    message: str = Field(..., description="Human-readable error message")
    field: str | None = Field(
        None, description="Field that caused the error (for validation errors)"
    )
    details: dict[str, Any] | None = Field(None, description="Additional error details")


class ResponseMetadata(BaseModel):
    """Metadata information for API responses."""

    timestamp: str = Field(
        default_factory=lambda: datetime.now().isoformat(),
        description="Response timestamp",
    )
    request_id: str | None = Field(None, description="Unique request identifier")
    version: str | None = Field(None, description="API version")
    execution_time: float | None = Field(
        None, description="Request execution time in seconds"
    )


class BaseResponse(BaseModel):
    """Base response model for all API responses."""

    success: bool = Field(..., description="Whether the request was successful")
    metadata: ResponseMetadata = Field(
        default_factory=cast(Callable[[], ResponseMetadata], ResponseMetadata),
        description="Response metadata",
    )


class SuccessResponse(BaseResponse):
    """Standard success response."""

    success: bool = Field(True, description="Success status")
    data: dict[str, Any] | list[Any] | str | int | float | bool | None = Field(
        None, description="Response data"
    )


class ErrorResponse(BaseResponse):
    """Standard error response."""

    success: bool = Field(False, description="Success status")
    data: Any | None = Field(None, description="Response data (null for errors)")
    error: ErrorDetail = Field(..., description="Error details")

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": False,
                "metadata": {
                    "timestamp": "2024-01-01T12:00:00Z",
                    "request_id": "req_123456789",
                    "version": "1.0.0",
                },
                "data": None,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid input data",
                    "field": "email",
                    "details": {"validation_type": "email_format"},
                },
            }
        }
    )


# Response models referenced in route `responses` (OpenAPI).
class Response200(SuccessResponse):
    """200 OK Response."""

    pass


class Response401(ErrorResponse):
    """401 Unauthorized Response."""

    def __init__(self, message: str = "Unauthorized", **kwargs):
        error = ErrorDetail(
            code=ErrorCode.UNAUTHORIZED, message=message, **kwargs.pop("error", {})
        )
        super().__init__(error=error, **kwargs)


class Response500(ErrorResponse):
    """500 Internal Server Error Response."""

    def __init__(self, message: str = "Internal server error", **kwargs):
        error = ErrorDetail(
            code=ErrorCode.INTERNAL_ERROR, message=message, **kwargs.pop("error", {})
        )
        super().__init__(error=error, **kwargs)
