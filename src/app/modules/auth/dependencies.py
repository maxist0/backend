"""FastAPI dependencies of the auth module: form login and the refresh cookie."""

import structlog
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordRequestForm

from app.core import DbSession
from app.core.errors import AuthenticationError
from app.modules.users import User


async def authenticate_form_user(
    request: Request,
    db: DbSession,
    form_data: OAuth2PasswordRequestForm = Depends(),
) -> User:
    """Authenticate via OAuth2 password form (used by /auth/login + Swagger)."""
    from app.modules.auth.service import authenticate_user

    user, error = await authenticate_user(
        db, form_data.username, form_data.password, request
    )
    if user is None:
        raise AuthenticationError(error or "Invalid username or password")

    # Stamp every subsequent log in this request with the authenticated user.
    structlog.contextvars.bind_contextvars(user_id=str(user.id))

    return user


async def get_refresh_token_from_cookie(request: Request) -> str:
    """Extract the raw refresh token from the httpOnly cookie."""
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise AuthenticationError("Refresh token missing")
    return refresh_token
