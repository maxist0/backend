# src/api/auth/schemas.py

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    EmailStr,
    Field,
    field_validator,
)
from datetime import datetime
import re
from typing import Annotated, Optional

# ---------------------------------------------------------------------------
# Pydantic Request / Response Schemas
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _reject_whitespace_and_control_chars(value: object) -> object:
    """Reject instead of silently stripping, e.g. a trailing newline.

    `EmailStr` strips surrounding whitespace and accepts the
    ``"Name <addr>"`` form; an address field must contain only the address.
    """
    if isinstance(value, str) and any(
        ch.isspace() or not ch.isprintable() for ch in value
    ):
        raise ValueError("Email must not contain whitespace or control characters")
    return value


# RFC 5321-valid address (email-validator via pydantic[email]), stored lower case.
NormalizedEmail = Annotated[
    EmailStr,
    BeforeValidator(_reject_whitespace_and_control_chars),
    AfterValidator(str.lower),
    Field(max_length=255),
]


class RegisterRequest(BaseModel):
    """User registration request."""

    username: str = Field(..., min_length=3, max_length=64)
    email: NormalizedEmail
    password: str = Field(..., min_length=8, max_length=128)
    display_name: Optional[str] = Field(None, max_length=128)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        if not _USERNAME_RE.match(v):
            raise ValueError(
                "Username may only contain letters, digits, hyphens, and underscores"
            )
        return v


class PasswordForgotRequest(BaseModel):
    """Request a password-reset email."""

    email: NormalizedEmail


class PasswordResetRequest(BaseModel):
    """Complete a password reset using the token from the email link."""

    token: str
    new_password: str = Field(..., min_length=8, max_length=128)


class EmailVerifyRequest(BaseModel):
    """Submit the email-verification token."""

    token: str


class ChangePasswordRequest(BaseModel):
    """Authenticated password change (requires current password)."""

    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class UpdateProfileRequest(BaseModel):
    """Update the current user's profile."""

    display_name: Optional[str] = Field(None, max_length=128)
    email: Optional[str] = Field(None, max_length=255)

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _EMAIL_RE.match(v):
            raise ValueError("Invalid email format")
        return v.lower()


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class UserResponse(BaseModel):
    """Public user representation returned by the API."""

    id: str
    username: str
    email: str
    roles: list[str]  # TODO Type Safety of roles rather than just strings
    permissions: list[str]  # TODO Type Safety of permissions rather than just strings
    display_name: Optional[str] = None
    profile_picture_url: Optional[str] = None
    is_active: bool
    is_email_verified: bool
    created_at: datetime
    last_login_at: Optional[datetime] = None
    preferences: Optional[dict] = None  # Placeholder for user preferences


class TokenResponse(BaseModel):
    """OAuth2-compatible access-token response."""

    access_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    """Decoded and validated JWT access-token payload."""

    user_id: str  # canonical UUID string (claim `sub`)
    username: Optional[str] = None
    jti: str
    # Refresh-token family id (claim `sid`); None for tokens issued before it existed.
    session_id: Optional[str] = None
