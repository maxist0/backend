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
    Settings,
    SettingsDep,
    Unauthorized,
    get_logger,
    problem_responses,
)
from app.core.security import TokenData
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
from app.modules.users import CurrentUser, User
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


def _accepted() -> Response:
    # FastAPI would render `None` as a `null` body; a 202 here has none.
    return Response(status_code=status.HTTP_202_ACCEPTED)


def _token_response(access_token: str, settings: Settings) -> TokenResponse:
    return TokenResponse(
        access_token=access_token,
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


@auth_router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register an account",
    responses=problem_responses(UserExistsError, 422),
)
async def register(
    body: RegisterRequest, request: Request, db: DbSession
) -> UserResponse:
    """Create an account and send a verification email to its address.

    The account can sign in right away; the address stays unverified until
    the link in the email is used.
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
    responses=problem_responses(InvalidCredentialsError, 422),
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
    """
    access_token, raw_refresh = await service.create_session_tokens(db, user, request)
    _set_refresh_cookie(response, raw_refresh, settings)
    logger.info("auth.user_logged_in")
    return LoginResponse(
        **_token_response(access_token, settings).model_dump(),
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

    No access token is needed.
    """
    await service.logout_session(db, raw_token, request)
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
    responses=problem_responses(InvalidRefreshTokenError),
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
    its family. No access token is needed.
    """
    access_token, new_raw_refresh = await service.refresh_session(
        db, raw_token, request
    )
    _set_refresh_cookie(response, new_raw_refresh, settings)
    return _token_response(access_token, settings)


@auth_router.post(
    "/password/forgot",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    summary="Request a password-reset email",
    responses=problem_responses(422),
)
async def password_forgot(
    body: PasswordForgotRequest, request: Request, db: DbSession
) -> Response:
    """Send a password-reset link if the address belongs to an account.

    The answer is the same whether or not it does.
    """
    await service.forgot_password(db, body.email, request)
    return _accepted()


@auth_router.post(
    "/password/reset",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Reset the password",
    responses=problem_responses(InvalidTokenError, 422),
)
async def password_reset(
    body: PasswordResetRequest, request: Request, db: DbSession
) -> None:
    """Set a new password with the token from the reset email.

    Every session of the account is signed out.
    """
    await service.reset_password(db, body.token, body.new_password, request)


@auth_router.post(
    "/email/verify",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Confirm an email address",
    responses=problem_responses(InvalidTokenError, EmailTakenError, 422),
)
async def email_verify(
    body: EmailVerifyRequest, request: Request, db: DbSession
) -> None:
    """Redeem the token of a verification or email-change link.

    A verification link marks the address verified. An email-change link
    switches the account to the new address (409 `email_taken` if another
    account took it meanwhile). Each token works once.
    """
    await service.verify_email(db, body.token, request)


@auth_router.post(
    "/email/verify/resend",
    status_code=status.HTTP_202_ACCEPTED,
    response_class=Response,
    summary="Resend the verification email",
    responses=problem_responses(EmailAlreadyVerifiedError, Unauthorized),
)
async def email_verify_resend(
    request: Request, db: DbSession, current_user: CurrentUser
) -> Response:
    """Send a new verification link to the signed-in user's address.

    Earlier links stop working.
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
    responses=problem_responses(WrongPasswordError, Unauthorized, 422),
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
    responses=problem_responses(
        WrongPasswordError, EmailUnchangedError, Unauthorized, EmailTakenError, 422
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
    new request replaces the previous link.
    """
    await service.request_email_change(
        db, current_user, body.current_password, body.new_email, request
    )
    return _accepted()


router = APIRouter()
router.include_router(auth_router)
router.include_router(me_router)
