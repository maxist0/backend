"""Request schemas of the auth module."""

import re
from typing import Annotated

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    EmailStr,
    Field,
    field_validator,
)


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


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

    username: str = Field(..., min_length=3, max_length=64)
    email: NormalizedEmail
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str | None = Field(None, max_length=128)

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
