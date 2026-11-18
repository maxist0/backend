"""Request and response schemas of the users module."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class UpdateProfileRequest(BaseModel):
    """Profile fields of the current user; omitted or null fields stay unchanged.

    The email address is changed through `PUT /users/me/email`.
    """

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(None, max_length=128)


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
    # Placeholder until user preferences exist. Any keeps the published schema
    # an open object (`additionalProperties: true`).
    preferences: dict[str, Any] | None = None
