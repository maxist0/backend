# src/common/models.py

from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field
from datetime import datetime
from enum import Enum


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
    field: Optional[str] = Field(None, description="Field that caused the error (for validation errors)")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional error details")


class PaginationInfo(BaseModel):
    """Pagination information for paginated responses."""
    total_records: int = Field(..., description="Total number of records")
    current_page: int = Field(..., description="Current page number")
    total_pages: int = Field(..., description="Total number of pages")
    limit: int = Field(..., description="Number of items per page")
    next_page: Optional[str] = Field(None, description="URL for the next page")
    prev_page: Optional[str] = Field(None, description="URL for the previous page")


class ResponseMetadata(BaseModel):
    """Metadata information for API responses."""
    timestamp: str = Field(default_factory=lambda: datetime.utcnow().isoformat(), description="Response timestamp")
    request_id: Optional[str] = Field(None, description="Unique request identifier")
    version: Optional[str] = Field(None, description="API version")
    execution_time: Optional[float] = Field(None, description="Request execution time in seconds")


class BaseResponse(BaseModel):
    """Base response model for all API responses."""
    success: bool = Field(..., description="Whether the request was successful")
    metadata: ResponseMetadata = Field(default_factory=ResponseMetadata, description="Response metadata")
    


class SuccessResponse(BaseResponse):
    """Standard success response."""
    success: bool = Field(True, description="Success status")
    data: Optional[Union[Dict[str, Any], List[Any], str, int, float, bool]] = Field(
        None, description="Response data"
    )
    error: Optional[ErrorDetail] = Field(None, description="Error details (null for success)")
    
    


class ErrorResponse(BaseResponse):
    """Standard error response."""
    success: bool = Field(False, description="Success status")
    data: Optional[Any] = Field(None, description="Response data (null for errors)")
    error: ErrorDetail = Field(..., description="Error details")
    
    class Config:
        schema_extra = {
            "example": {
                "success": False,
                "metadata": {
                    "timestamp": "2024-01-01T12:00:00Z",
                    "request_id": "req_123456789",
                    "version": "1.0.0"
                },
                "data": None,
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Invalid input data",
                    "field": "email",
                    "details": {
                        "validation_type": "email_format"
                    }
                }
            }
        }


class PaginatedResponse(BaseResponse):
    """Paginated response with separate pagination field."""
    success: bool = Field(True, description="Success status")
    data: List[Any] = Field(..., description="List of items")
    pagination: PaginationInfo = Field(..., description="Pagination information")
    error: Optional[ErrorDetail] = Field(None, description="Error details (null for success)")
    
    class Config:
        schema_extra = {
            "example": {
                "success": True,
                "data": [
                    {"id": 1, "name": "Item A"},
                    {"id": 2, "name": "Item B"}
                ],
                "pagination": {
                    "total_records": 150,
                    "current_page": 1,
                    "total_pages": 8,
                    "limit": 20,
                    "next_page": "/users?page=2"
                },
                "metadata": {
                    "timestamp": "2024-01-01T12:00:00Z",
                    "request_id": "req_123456789",
                    "version": "1.0.0"
                },
                "error": None
            }
        }


# Convenience response models for common HTTP status codes
class Response200(SuccessResponse):
    """200 OK Response."""
    pass


class Response201(SuccessResponse):
    """201 Created Response."""
    pass


class Response400(ErrorResponse):
    """400 Bad Request Response."""
    def __init__(self, message: str = "Bad Request", field: Optional[str] = None, **kwargs):
        error = ErrorDetail(
            code=ErrorCode.BAD_REQUEST,
            message=message,
            field=field,
            **kwargs.pop('error', {})
        )
        super().__init__(error=error, **kwargs)


class Response401(ErrorResponse):
    """401 Unauthorized Response."""
    def __init__(self, message: str = "Unauthorized", **kwargs):
        error = ErrorDetail(
            code=ErrorCode.UNAUTHORIZED,
            message=message,
            **kwargs.pop('error', {})
        )
        super().__init__(error=error, **kwargs)


class Response403(ErrorResponse):
    """403 Forbidden Response."""
    def __init__(self, message: str = "Forbidden", **kwargs):
        error = ErrorDetail(
            code=ErrorCode.FORBIDDEN,
            message=message,
            **kwargs.pop('error', {})
        )
        super().__init__(error=error, **kwargs)


class Response404(ErrorResponse):
    """404 Not Found Response."""
    def __init__(self, message: str = "Resource not found", **kwargs):
        error = ErrorDetail(
            code=ErrorCode.NOT_FOUND,
            message=message,
            **kwargs.pop('error', {})
        )
        super().__init__(error=error, **kwargs)


class Response409(ErrorResponse):
    """409 Conflict Response."""
    def __init__(self, message: str = "Resource conflict", **kwargs):
        error = ErrorDetail(
            code=ErrorCode.CONFLICT,
            message=message,
            **kwargs.pop('error', {})
        )
        super().__init__(error=error, **kwargs)


class Response429(ErrorResponse):
    """429 Rate Limited Response."""
    def __init__(self, message: str = "Rate limit exceeded", **kwargs):
        error = ErrorDetail(
            code=ErrorCode.RATE_LIMITED,
            message=message,
            **kwargs.pop('error', {})
        )
        super().__init__(error=error, **kwargs)


class Response500(ErrorResponse):
    """500 Internal Server Error Response."""
    def __init__(self, message: str = "Internal server error", **kwargs):
        error = ErrorDetail(
            code=ErrorCode.INTERNAL_ERROR,
            message=message,
            **kwargs.pop('error', {})
        )
        super().__init__(error=error, **kwargs)