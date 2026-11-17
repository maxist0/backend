"""FastAPI dependencies of the users module: the current user.

Other modules use only the `CurrentUser` and `OptionalUser` aliases. The
token is decoded by the core (`app.core.security`), so resolving the user
needs nothing from the auth module.
"""

import uuid
from typing import Annotated, Any

import structlog
from fastapi import Depends
from fastapi.openapi.models import OAuth2 as OAuth2Model
from fastapi.openapi.models import OAuthFlowPassword, OAuthFlows
from fastapi.security import OAuth2PasswordBearer

from app.core import DbSession, Unauthorized, get_logger, get_settings
from app.core.security import TokenData, decode_token
from app.modules.users import crud
from app.modules.users.models import User

logger = get_logger(__name__)


class _PasswordBearer(OAuth2PasswordBearer):
    """Bearer scheme whose Swagger token URLs follow `API_PREFIX`.

    Importing a module reads no settings, so the URLs cannot be fixed in
    `__init__`. FastAPI reads `model` only while it builds the OpenAPI
    document (`fastapi/openapi/utils.py`), when the settings exist.
    """

    def __init__(self, *, auto_error: bool) -> None:
        super().__init__(
            tokenUrl="/auth/login",
            scheme_name="OAuth2PasswordBearer",
            auto_error=auto_error,
        )

    # `SecurityBase` declares `model` as a plain attribute; a property with a
    # setter is the same interface at runtime, mypy only rejects the form.
    @property  # type: ignore[override]
    def model(self) -> OAuth2Model:
        prefix = get_settings().API_PREFIX
        password = OAuthFlowPassword(
            tokenUrl=f"{prefix}/auth/login", refreshUrl=f"{prefix}/auth/refresh"
        )
        return OAuth2Model(flows=OAuthFlows(password=password))

    @model.setter
    def model(self, value: Any) -> None:
        """Ignore the model `OAuth2PasswordBearer.__init__` assigns."""


oauth2_scheme = _PasswordBearer(auto_error=True)
optional_oauth2_scheme = _PasswordBearer(auto_error=False)


async def get_current_token_data(
    token: str = Depends(oauth2_scheme),
) -> TokenData:
    """Decode the Bearer token and return its payload."""
    token_data = decode_token(token)
    if not token_data:
        raise Unauthorized("Invalid or expired access token")
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
        raise Unauthorized("User not found")
    if not user.is_active:
        raise Unauthorized("Account is deactivated")
    if user.is_admin_locked():
        raise Unauthorized("Account is locked")
    # Stamp every subsequent log in this request with the authenticated user
    # (user_id only: no usernames or emails in logs).
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


async def get_optional_user(
    db: DbSession,
    token: str | None = Depends(optional_oauth2_scheme),
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
