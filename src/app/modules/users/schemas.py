"""Request and response schemas of the users module."""

import re
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class UpdateProfileRequest(BaseModel):
    """Update the current user's profile."""

    display_name: str | None = Field(None, max_length=128)
    email: str | None = Field(None, max_length=255)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email format")
        return v.lower()


class UserResponse(BaseModel):
    """Public user representation returned by the API."""

    id: str
    username: str
    email: str
    # Plain strings until roles and permissions are decided (RBAC is open).
    roles: list[str]
    permissions: list[str]
    display_name: str | None = None
    profile_picture_url: str | None = None
    is_active: bool
    is_email_verified: bool
    created_at: datetime
    last_login_at: datetime | None = None
    preferences: dict | None = None  # Placeholder for user preferences
