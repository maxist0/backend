# src/api/auth/models.py

"""
Simple JWT authentication models.
"""

from datetime import datetime
from pydantic import BaseModel, Field
import uuid


class TokenData(BaseModel):
    """Token data model for JWT payload."""

    user_id: str
    username: str
    jti: str  # JWT ID for token revocation

# Deprecated
class TokenPair(BaseModel):
    """JWT token pair model."""
    refresh_token: str
    access_token: str
    token_type: str = "bearer"


class AccessToken(BaseModel):
    """Access token model for response."""

    access_token: str
    token_type: str = "bearer"


class RefreshToken(BaseModel):
    """Refresh token model for storage."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    username: str
    token: str
    expires_at: datetime
    revoked: bool = False
    revoke_reason: str | None = None     # e.g. "logout", "reuse_detected", "admin"

    # Lifecycle
    created_at: datetime = Field(default_factory=datetime.utcnow)
    revoked_at: datetime | None = None

    # Context / fingerprinting
    device_id: str | None = None
    device_name: str | None = None        # e.g. "Chrome on macOS"
    ip_address: str | None = None
    user_agent: str | None = None


class TokenRequest(BaseModel):
    """Simple token request model."""

    username: str


class RefreshTokenRequest(BaseModel):
    """Refresh token request model."""

    refresh_token: str


class RegisterRequest(BaseModel):
    """User registration request model."""

    username: str
    password: str


class LoginRequest(BaseModel):
    """User login request model."""

    username: str
    password: str


class LogoutRequest(BaseModel):
    """Logout request model – revokes the provided refresh token."""

    refresh_token: str


class UserRecord(BaseModel):
    """In-memory user record."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    username: str
    password_hash: str
