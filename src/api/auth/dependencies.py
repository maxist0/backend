# src/api/auth/dependencies.py

"""
FastAPI dependency injection for authentication.
"""

import uuid
from typing import Optional

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from src.api.auth import crud
from src.api.auth.jwt_utils import decode_token
from src.api.auth.models import TokenData, User
from src.common.dependencies import DbSession
from src.common.logging import get_logger

logger = get_logger(__name__)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login", refreshUrl="/auth/refresh", auto_error=True
)


class AuthenticationError(HTTPException):
    def __init__(self, detail: str = "Authentication failed"):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


# ---------------------------------------------------------------------------
# Access-token dependencies
# ---------------------------------------------------------------------------

async def get_current_token_data(
    token: str = Depends(oauth2_scheme),
) -> TokenData:
    """Decode the Bearer token and return its payload."""
    token_data = decode_token(token)
    if not token_data:
        raise AuthenticationError("Invalid or expired access token")
    return token_data


async def get_current_user(
    db: DbSession,
    token_data: TokenData = Depends(get_current_token_data),
) -> User:
    """Return the full User ORM object for the currently authenticated user."""
    user = await crud.get_user_by_id(db, uuid.UUID(token_data.user_id))
    if user is None:
        raise AuthenticationError("User not found")
    if not user.is_active:
        raise AuthenticationError("Account is deactivated")
    return user


async def get_optional_user(
    db: DbSession,
    token: Optional[str] = Depends(
        OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)
    ),
) -> Optional[User]:
    """Return User if a valid token is present, None otherwise."""
    if not token:
        return None
    try:
        token_data = decode_token(token)
        if not token_data:
            return None
        user = await crud.get_user_by_id(db, uuid.UUID(token_data.user_id))
        return user if user and user.is_active else None
    except Exception as e:
        logger.warning(f"Error in optional authentication: {e}")
        return None


# ---------------------------------------------------------------------------
# Form-based authentication (OAuth2 password flow for /auth/login)
# ---------------------------------------------------------------------------

async def authenticate_form_user(
    request: Request,
    db: DbSession,
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> User:
    """Authenticate via OAuth2 password form (used by /auth/login + Swagger)."""
    from src.api.auth.service import authenticate_user

    user, error = await authenticate_user(
        db, form_data.username, form_data.password, request
    )
    if user is None:
        raise AuthenticationError(error or "Invalid username or password")
    return user


# ---------------------------------------------------------------------------
# Refresh-token cookie extraction
# ---------------------------------------------------------------------------

async def get_refresh_token_from_cookie(request: Request) -> str:
    """Extract the raw refresh token from the httpOnly cookie."""
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise AuthenticationError("Refresh token missing")
    logger.debug(f"Refresh token found in cookie: {refresh_token[:8]}...")
    return refresh_token