"""FastAPI dependencies of the users module: the current user.

Other modules use the `CurrentUser` and `OptionalUser` aliases, the
rate-limit key `by_user` and the `Idempotent` route marker. The
token is decoded by the core (`app.core.security`), so resolving the user
needs nothing from the auth module.
"""

import uuid
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.openapi.models import OAuth2 as OAuth2Model
from fastapi.openapi.models import OAuthFlowPassword, OAuthFlows
from fastapi.security import OAuth2PasswordBearer
from fastapi.security.utils import get_authorization_scheme_param
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import (
    DbSession,
    TokenData,
    Unauthorized,
    decode_token,
    get_logger,
    get_settings,
    idempotent,
)
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

    @property
    def model(self) -> OAuth2Model:
        prefix = get_settings().API_PREFIX
        password = OAuthFlowPassword(
            tokenUrl=f"{prefix}/auth/login", refreshUrl=f"{prefix}/auth/refresh"
        )
        return OAuth2Model(flows=OAuthFlows(password=password))

    @model.setter
    def model(self, value: object) -> None:
        """Ignore the model `OAuth2PasswordBearer.__init__` assigns."""


oauth2_scheme = _PasswordBearer(auto_error=True)
optional_oauth2_scheme = _PasswordBearer(auto_error=False)


async def get_current_token_data(
    token: Annotated[str, Depends(oauth2_scheme)],
) -> TokenData:
    """Decode the Bearer token and return its payload."""
    token_data = decode_token(token)
    if not token_data:
        raise Unauthorized("Invalid or expired access token")
    return token_data


async def get_current_user(
    db: DbSession,
    token_data: Annotated[TokenData, Depends(get_current_token_data)],
) -> User:
    """Return the full User ORM object for the currently authenticated user.

    Rejects deleted, deactivated and admin-locked accounts, so those take
    effect immediately and not only at the next refresh. A timed lock from
    failed logins blocks only the password login and is not checked here.
    Rejects tokens issued before the last password change or reset: the
    client refreshes, and only sessions the change kept still can.
    """
    user = await crud.get_user_by_id(db, uuid.UUID(token_data.user_id))
    if user is None:
        raise Unauthorized("User not found")
    if not user.is_active:
        raise Unauthorized("Account is deactivated")
    if user.is_admin_locked():
        raise Unauthorized("Account is locked")
    if user.password_changed_after(token_data.issued_at):
        logger.info("auth.access_token_outdated", user_id=str(user.id))
        raise Unauthorized("Invalid or expired access token")
    # Stamp every subsequent log in this request with the authenticated user
    # (user_id only: no usernames or emails in logs).
    structlog.contextvars.bind_contextvars(user_id=str(user.id))
    return user


async def get_optional_user(
    db: DbSession,
    token: Annotated[str | None, Depends(optional_oauth2_scheme)],
) -> User | None:
    """Return User if a valid token is present, None otherwise."""
    if not token:
        return None
    return await _user_of_token(db, token, log_outdated=True)


async def _user_of_token(
    db: AsyncSession, token: str, *, log_outdated: bool
) -> User | None:
    """The user of a valid access token, with the checks of `CurrentUser`.

    `log_outdated` logs `auth.access_token_outdated` for a token issued
    before the last password change.
    """
    try:
        token_data = decode_token(token)
        if not token_data:
            return None
        user = await crud.get_user_by_id(db, uuid.UUID(token_data.user_id))
    except Exception as e:  # noqa: BLE001 - an optional user never fails the request
        logger.warning("auth.optional_auth_failed", error_type=type(e).__name__)
        return None
    if user is None or not user.is_active or user.is_admin_locked():
        return None
    if user.password_changed_after(token_data.issued_at):
        if log_outdated:
            logger.info("auth.access_token_outdated", user_id=str(user.id))
        return None
    return user


# The authenticated, active, not admin-locked user; 401 otherwise.
CurrentUser = Annotated[User, Depends(get_current_user)]
# The user of a valid bearer token, or None without one.
OptionalUser = Annotated[User | None, Depends(get_optional_user)]


def by_user(user: CurrentUser) -> str:
    """Rate-limit key of a route: the signed-in user's id.

    For `rate_limit(..., key=by_user)` from `app.core`. It resolves
    `CurrentUser` first, so a request without a valid token gets 401 and is
    not counted; FastAPI reuses the resolved user for the route.
    """
    return str(user.id)


async def idempotency_scope(request: Request, db: DbSession) -> str | None:
    """Idempotency scope of a request: the signed-in user's id.

    None for an anonymous request (no or an invalid token), which the core
    scopes by route. The checks are those of `OptionalUser`, so a
    deactivated or locked account or an outdated token never reaches the
    user's stored responses. The bearer token is read like the OAuth2
    scheme reads it, but not through it: the scheme would mark every
    marked route as secured in OpenAPI, the anonymous ones too.
    """
    scheme, token = get_authorization_scheme_param(request.headers.get("Authorization"))
    if scheme.lower() != "bearer" or not token:
        return None
    # Not logged here: on a route that requires the user, `CurrentUser`
    # refuses the same token and logs it once.
    user = await _user_of_token(db, token, log_outdated=False)
    return str(user.id) if user is not None else None


# Marks a route as accepting `Idempotency-Key`: `dependencies=[..., Idempotent]`
# after the route's rate limits. See `app.core.idempotency`.
Idempotent = idempotent(scope=idempotency_scope)
