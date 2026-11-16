# src/app/api/user/schemas.py

"""
Pydantic request/response schemas for /user endpoints.
Shared with auth schemas where possible.
"""

from app.api.auth.schemas import (
    ChangePasswordRequest,
    UpdateProfileRequest,
    UserResponse,
)

__all__ = ["ChangePasswordRequest", "UpdateProfileRequest", "UserResponse"]
