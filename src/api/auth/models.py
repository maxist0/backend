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
    type: str  # "access" or "refresh"


class TokenPair(BaseModel):
    """JWT token pair model."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshToken(BaseModel):
    """Refresh token model for storage."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    user_id: str
    username: str
    token: str
    expires_at: datetime
    revoked: bool = False


class TokenRequest(BaseModel):
    """Simple token request model."""

    username: str


class RefreshTokenRequest(BaseModel):
    """Refresh token request model."""

    refresh_token: str
