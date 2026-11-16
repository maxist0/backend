"""FastAPI dependencies of the users module: the current user.

Other modules use only the `CurrentUser` and `OptionalUser` aliases. The
token is decoded by the core (`app.core.security`), so resolving the user
needs nothing from the auth module.
"""

import uuid
from typing import Annotated

import structlog
from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer

from app.core import DbSession, get_logger
from app.core.errors import AuthenticationError
from app.core.security import TokenData, decode_token
from app.modules.users import crud
from app.modules.users.models import User

logger = get_logger(__name__)

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl="/auth/login", refreshUrl="/auth/refresh", auto_error=True
)


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
    """Return the full User ORM object for the currently authenticated user.

    Rejects deleted, deactivated and admin-locked accounts, so those take
    effect immediately and not only at the next refresh. A timed lock from
    failed logins blocks only the password login and is not checked here.
    """
    user = await crud.get_user_by_id(db, uuid.UUID(token_data.user_id))
    if user is None:
        raise AuthenticationError("User not found")
    if not user.is_active:
        raise AuthenticationError("Account is deactivated")
    if user.is_admin_locked():
        raise AuthenticationError("Account is locked")
    # Stamp every subsequent log in this request with the authenticated user
    # (user_id only: no usernames or emails in logs).
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


async def get_optional_user(
    db: DbSession,
    token: str | None = Depends(
        OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)
    ),
) -> User | None:
    """Return User if a valid token is present, None otherwise."""
    if not token:
        return None
    try:
        token_data = decode_token(token)
        if not token_data:
            return None
        user = await crud.get_user_by_id(db, uuid.UUID(token_data.user_id))
        if user is None or not user.is_active or user.is_admin_locked():
            return None
        return user
    except Exception as e:
        logger.warning("auth.optional_auth_failed", error_type=type(e).__name__)
        return None


# The authenticated, active, not admin-locked user; 401 otherwise.
CurrentUser = Annotated[User, Depends(get_current_user)]
# The user of a valid bearer token, or None without one.
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
