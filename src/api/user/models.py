# src/api/user/models.py

"""
Pydantic request/response schemas for /user endpoints.
ORM models live in src.api.auth.models (shared).
"""

from src.api.auth.models import (
    ChangePasswordRequest,
    UpdateProfileRequest,
    UserResponse,
)

__all__ = ["ChangePasswordRequest", "UpdateProfileRequest", "UserResponse"]
