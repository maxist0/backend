"""FastAPI dependencies of the auth module: form login and the refresh cookie."""

from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import OAuth2PasswordRequestForm

from app.core import DbSession, by_ip, hit
from app.modules.auth.errors import InvalidRefreshTokenError
from app.modules.auth.service import authenticate_user
from app.modules.users import User

REFRESH_COOKIE = "refresh_token"


async def authenticate_form_user(
    request: Request,
    db: DbSession,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
) -> User:
    """Authenticate via the OAuth2 password form (`/auth/login`, Swagger).

    Raises:
        RateLimitedError: More than 10 attempts in 15 minutes for this
            username from this client address.
    """
    # Before any lookup, admission slot or Argon2: a refused guess costs no
    # hash, and the count is the same whether the account exists or not.
    # Lower case, as the lookup compares usernames.
    await hit(
        "auth.login.ip_username",
        "10/15 minutes",
        key=f"{by_ip(request)}\0{form_data.username.lower()}",
    )
    user = await authenticate_user(db, form_data.username, form_data.password, request)

    # Stamp every subsequent log in this request with the authenticated user.
    structlog.contextvars.bind_contextvars(user_id=str(user.id))

    return user


async def get_refresh_token_from_cookie(request: Request) -> str:
    """Extract the raw refresh token from the httpOnly cookie."""
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        raise InvalidRefreshTokenError("Refresh token missing")
    return refresh_token
