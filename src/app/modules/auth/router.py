"""HTTP routes of the auth module.

`auth_router` (`/auth`): register, login, refresh, logout, logout-all,
password forgot and reset, email verification and the 2FA stubs.
`me_router` (`/users`, tag `users`): password and email change of the
signed-in user; they need credentials and email tokens, so they live here
and not in the users module. `router` is the module router combining both.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status

from app.core import (
    DbSession,
    RateLimitedError,
    Settings,
    SettingsDep,
    TokenData,
    Unauthorized,
    by_ip,
    by_ipv6_48,
    get_logger,
    problem_responses,
    rate_limit,
)
from app.modules.auth import service
from app.modules.auth.dependencies import (
    REFRESH_COOKIE,
    authenticate_form_user,
    get_refresh_token_from_cookie,
)
from app.modules.auth.errors import (
    EmailAlreadyVerifiedError,
    EmailTakenError,
    EmailUnchangedError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidTokenError,
    SessionNotFoundError,
    TwoFactorNotImplementedError,
    UserExistsError,
    WrongPasswordError,
)
from app.modules.auth.schemas import (
    ChangeEmailRequest,
    ChangePasswordRequest,
    EmailVerifyRequest,
    LoginResponse,
    PasswordForgotRequest,
    PasswordResetRequest,
    RegisterRequest,
    TokenResponse,
)
from app.modules.users import CurrentUser, User, by_user
from app.modules.users.dependencies import get_current_token_data
from app.modules.users.schemas import UserResponse
from app.modules.users.service import user_to_response

logger = get_logger(__name__)
auth_router = APIRouter(prefix="/auth", tags=["authentication"])
me_router = APIRouter(prefix="/users", tags=["users"])

RefreshToken = Annotated[str, Depends(get_refresh_token_from_cookie)]


def _cookie_path(settings: Settings) -> str:
    # The browser sends the refresh cookie only to the auth routes.
    return f"{settings.API_PREFIX}/auth"


def _set_refresh_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=REFRESH_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        path=_cookie_path(settings),
    )


def _delete_refresh_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(key=REFRESH_COOKIE, path=_cookie_path(settings))


def _delete_refresh_cookie_on(
    exc: InvalidRefreshTokenError, settings: Settings
) -> None:
    # The error response replaces the injected `response`, so the deletion
    # travels in the error's headers.
    cleared = Response()
    _delete_refresh_cookie(cleared, settings)
    exc.headers["Set-Cookie"] = cleared.headers["set-cookie"]


def _accepted() -> Response:
    # FastAPI would render `None` as a `null` body; a 202 here has none.
    return Response(status_code=status.HTTP_202_ACCEPTED)


def _token_response(tokens: service.SessionTokens) -> TokenResponse:
    return TokenResponse(access_token=tokens.access_token, expires_in=tokens.expires_in)


@auth_router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register an account",
    dependencies=[rate_limit("auth.register.ip", "5/hour", key=by_ip)],
    responses=problem_responses(UserExistsError, 422, RateLimitedError),
)
async def register(
    body: RegisterRequest, request: Request, db: DbSession
) -> UserResponse:
    """Create an account and send a verification email to its address.

    The account can sign in right away; the address stays unverified until
    the link in the email is used. At most 5 registrations per hour from one
    client address; more get 429 with `Retry-After`.
    """
    user = await service.register_user(
        db=db,
        username=body.username,
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        request=request,
    )
    return user_to_response(user)


@auth_router.post(
    "/login",
    status_code=status.HTTP_200_OK,
    summary="Sign in",
    dependencies=[
        rate_limit("auth.login.ip", "20/minute", key=by_ip),
        rate_limit("auth.login.ipv6_48", "100/minute", key=by_ipv6_48),
    ],
    responses=problem_responses(InvalidCredentialsError, 422, RateLimitedError),
)
async def login(
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    user: Annotated[User, Depends(authenticate_form_user)],
) -> LoginResponse:
    """Sign in with the OAuth2 password form (`username`, `password`).

    Returns an access token and the user. The refresh token is set as an
    httpOnly cookie for the auth routes. A wrong password, an unknown user and
    a locked or deactivated account all get the same 401.

    At most 20 attempts per minute from one client address (an IPv6 client
    per /64, and 100 per minute per /48), and 10 per 15 minutes for one
    username from one address, whether the account exists or not; more get
    429 with `Retry-After`.
    """
    tokens = await service.create_session_tokens(db, user, request)
    _set_refresh_cookie(response, tokens.refresh_token, settings)
    logger.info("auth.user_logged_in")
    return LoginResponse(
        **_token_response(tokens).model_dump(),
        user=user_to_response(user),
    )


@auth_router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Sign out this session",
    responses=problem_responses(SessionNotFoundError, InvalidRefreshTokenError),
)
async def logout(
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    raw_token: RefreshToken,
) -> None:
    """Revoke the session of the refresh cookie and clear the cookie.

    No access token is needed. A refresh token that was already rotated is
    treated like reuse on refresh: all sessions of its family are revoked,
    the answer is 401 and the cookie is cleared.
    """
    try:
        await service.logout_session(db, raw_token, request)
    except InvalidRefreshTokenError as exc:
        _delete_refresh_cookie_on(exc, settings)
        raise
    _delete_refresh_cookie(response, settings)


@auth_router.post(
    "/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Sign out on all devices",
    responses=problem_responses(Unauthorized),
)
async def logout_all(
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    current_user: CurrentUser,
) -> None:
    """Revoke every session of the signed-in user and clear the refresh cookie.

    Access tokens already issued stay valid until they expire
    (`ACCESS_TOKEN_EXPIRE_MINUTES`).
    """
    await service.logout_all_sessions(db, current_user.id, request)
    _delete_refresh_cookie(response, settings)


@auth_router.post(
    "/refresh",
    status_code=status.HTTP_200_OK,
    summary="Get a new access token",
    dependencies=[rate_limit("auth.refresh.ip", "60/minute", key=by_ip)],
    responses=problem_responses(InvalidRefreshTokenError, RateLimitedError),
)
async def refresh_tokens(
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    raw_token: RefreshToken,
) -> TokenResponse:
    """Exchange the refresh cookie for a new access token.

    The refresh token is rotated: the cookie is replaced and the old token
    stops working. Presenting a rotated token again revokes all sessions of
    its family. No access token is needed. A refused cookie (unknown,
    expired, revoked or reused) is cleared with the 401.

    At most 60 refreshes per minute from one client address and 10 per
    minute for one login session; more get 429 with `Retry-After`, and the
    cookie stays valid.
    """
    try:
        tokens = await service.refresh_session(db, raw_token, request)
    except InvalidRefreshTokenError as exc:
        _delete_refresh_cookie_on(exc, settings)
        raise
    _set_refresh_cookie(response, tokens.refresh_token, settings)
    return _token_response(tokens)


@auth_router.post(
    "/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    summary="Request a password-reset email",
    dependencies=[
        rate_limit("auth.password_forgot.ip", "5/15 minutes", key=by_ip),
        rate_limit("auth.password_forgot.ipv6_48", "25/15 minutes", key=by_ipv6_48),
    ],
    responses=problem_responses(422, RateLimitedError),
)
async def password_forgot(
    body: PasswordForgotRequest, request: Request, db: DbSession
) -> Response:
    """Send a password-reset link if the address belongs to an account.

    The answer is the same whether or not it does. At most 5 requests per
    15 minutes from one client address (an IPv6 client per /64, and 25 per
    /48) and 3 per hour for one email address, whether it belongs to an
    account or not; more get 429 with `Retry-After`.
    """
    await service.forgot_password(db, body.email, request)
    return _accepted()


@auth_router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Reset the password",
    dependencies=[rate_limit("auth.password_reset.ip", "10/15 minutes", key=by_ip)],
    responses=problem_responses(InvalidTokenError, 422, RateLimitedError),
)
async def password_reset(
    body: PasswordResetRequest, request: Request, db: DbSession
) -> None:
    """Set a new password with the token from the reset email.

    Every session of the account is signed out. At most 10 attempts per 15
    minutes from one client address; more get 429 with `Retry-After`.
    """
    await service.reset_password(db, body.token, body.new_password, request)


@auth_router.post(
    "/email/verify",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Confirm an email address",
    dependencies=[rate_limit("auth.email_verify.ip", "10/15 minutes", key=by_ip)],
    responses=problem_responses(
        InvalidTokenError, EmailTakenError, 422, RateLimitedError
    ),
)
async def email_verify(
    body: EmailVerifyRequest, request: Request, db: DbSession
) -> None:
    """Redeem the token of a verification or email-change link.

    A verification link marks the address verified. An email-change link
    switches the account to the new address (409 `email_taken` if another
    account took it meanwhile). Each token works once. At most 10 attempts
    per 15 minutes from one client address; more get 429 with `Retry-After`.
    """
    await service.verify_email(db, body.token, request)


@auth_router.post(
    "/email/verify/resend",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    summary="Resend the verification email",
    dependencies=[rate_limit("auth.email_verify_resend.user", "3/hour", key=by_user)],
    responses=problem_responses(
        EmailAlreadyVerifiedError, Unauthorized, RateLimitedError
    ),
)
async def email_verify_resend(
    request: Request, db: DbSession, current_user: CurrentUser
) -> Response:
    """Send a new verification link to the signed-in user's address.

    Earlier links stop working. At most 3 requests per hour per user and 3
    per hour to one address; more get 429 with `Retry-After`.
    """
    await service.resend_verification_email(db, current_user, request)
    return _accepted()


@auth_router.post(
    "/2fa/enable",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Enable two-factor authentication (not implemented)",
    responses=problem_responses(TwoFactorNotImplementedError),
)
async def enable_2fa() -> None:
    """Not implemented yet; always answers 501."""
    raise TwoFactorNotImplementedError()


@auth_router.post(
    "/2fa/disable",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Disable two-factor authentication (not implemented)",
    responses=problem_responses(TwoFactorNotImplementedError),
)
async def disable_2fa() -> None:
    """Not implemented yet; always answers 501."""
    raise TwoFactorNotImplementedError()


@auth_router.post(
    "/2fa/verify",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Verify a two-factor code (not implemented)",
    responses=problem_responses(TwoFactorNotImplementedError),
)
async def verify_2fa() -> None:
    """Not implemented yet; always answers 501."""
    raise TwoFactorNotImplementedError()


@me_router.put(
    "/me/password",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Change the password",
    dependencies=[rate_limit("auth.password_change.user", "5/15 minutes", key=by_user)],
    responses=problem_responses(
        WrongPasswordError, Unauthorized, 422, RateLimitedError
    ),
)
async def change_my_password(
    body: ChangePasswordRequest,
    request: Request,
    db: DbSession,
    token_data: Annotated[TokenData, Depends(get_current_token_data)],
    current_user: CurrentUser,
) -> None:
    """Change the signed-in user's password; requires the current password.

    Every other session of the user is signed out; the calling session stays.
    At most 5 attempts per 15 minutes per user; more get 429 with
    `Retry-After`.
    """
    await service.change_password(
        db=db,
        user_id=current_user.id,
        current_password=body.current_password,
        new_password=body.new_password,
        request=request,
        keep_session_family=(
            uuid.UUID(token_data.session_id) if token_data.session_id else None
        ),
    )


@me_router.put(
    "/me/email",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    summary="Change the email address",
    dependencies=[rate_limit("auth.email_change.user", "5/15 minutes", key=by_user)],
    responses=problem_responses(
        WrongPasswordError,
        EmailUnchangedError,
        Unauthorized,
        EmailTakenError,
        422,
        RateLimitedError,
    ),
)
async def change_my_email(
    body: ChangeEmailRequest,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
) -> Response:
    """Request a new email address; requires the current password.

    A confirmation link goes to the new address. The account keeps its
    current address until the link is used (`POST /auth/email/verify`); a
    new request replaces the previous link. At most 5 attempts per 15
    minutes per user, and 3 links per hour to one new address from all
    accounts together (counted before the address is checked); more get 429
    with `Retry-After`.
    """
    await service.request_email_change(
        db, current_user, body.current_password, body.new_email, request
    )
    return _accepted()


router = APIRouter()
router.include_router(auth_router)
router.include_router(me_router)
