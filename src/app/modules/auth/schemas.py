"""Request and response schemas of the auth module."""

import re
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
)

from app.modules.users.schemas import UserResponse

# Matched with `fullmatch`: `$` in `match` would accept a trailing newline.
_USERNAME_RE = re.compile(r"[a-zA-Z0-9_-]+")


def is_username(value: str) -> bool:
    """Tell whether `value` is a well-formed username (ASCII only).

    The database compares usernames with `lower()`, which also folds some
    non-ASCII characters onto ASCII letters (the Kelvin sign onto `k`), so a
    login name is looked up only when this holds.
    """
    return _USERNAME_RE.fullmatch(value) is not None


def _reject_whitespace_and_control_chars(value: object) -> object:
    """Reject instead of silently stripping, e.g. a trailing newline.

    `EmailStr` strips surrounding whitespace and accepts the
    `"Name <addr>"` form; an address field must contain only the address.
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

    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=3, max_length=64)
    email: NormalizedEmail
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str | None = Field(None, max_length=128)

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        """Allow ASCII letters, digits, hyphens and underscores only."""
        if not is_username(v):
            raise ValueError(
                "Username may only contain letters, digits, hyphens, and underscores"
            )
        return v


class PasswordForgotRequest(BaseModel):
    """Request a password-reset email."""

    model_config = ConfigDict(extra="forbid")

    email: NormalizedEmail


class PasswordResetRequest(BaseModel):
    """Complete a password reset using the token from the email link."""

    model_config = ConfigDict(extra="forbid")

    token: str
    new_password: str = Field(..., min_length=8, max_length=128)


class EmailVerifyRequest(BaseModel):
    """Submit the token of a verification or email-change link."""

    model_config = ConfigDict(extra="forbid")

    token: str


class ChangePasswordRequest(BaseModel):
    """Authenticated password change (requires current password)."""

    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str = Field(..., min_length=8, max_length=128)


class ChangeEmailRequest(BaseModel):
    """Authenticated email change (requires current password)."""

    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_email: NormalizedEmail


class TokenResponse(BaseModel):
    """A new access token (RFC 6749 section 5.1)."""

    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105 - RFC 6749 token type
    expires_in: int = Field(description="Lifetime of the access token in seconds.")


class LoginResponse(TokenResponse):
    """A new access token and the signed-in user."""

    user: UserResponse
