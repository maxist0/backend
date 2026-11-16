"""HTTP routes of the auth module.

`auth_router` (`/auth`): register, login, refresh, logout, logout-all,
password forgot and reset, email verification and the 2FA stubs.
`me_router` (`/user`, tag `user`): the profile update (it can change the
email, which needs an email token) and the password change of the signed-in
user. `router` is the module router combining both.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from app.core import DbSession, get_logger, log_unexpected_error
from app.core.responses import (
    created_response,
    error_response,
    ok_response,
    success_response,
)
from app.core.schemas import ErrorCode, Response200, Response401, Response500
from app.core.security import TokenData
from app.modules.auth.dependencies import (
    authenticate_form_user,
    get_refresh_token_from_cookie,
)
from app.modules.auth.schemas import (
    ChangePasswordRequest,
    EmailVerifyRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    RegisterRequest,
)
from app.modules.auth.service import (
    add_refresh_token_to_cookie,
    change_password,
    create_session_tokens,
    delete_refresh_token_cookie,
    forgot_password,
    logout_all_sessions,
    logout_session,
    refresh_session,
    register_user,
    resend_verification_email,
    reset_password,
    update_profile,
    verify_email,
)
from app.modules.users import CurrentUser, User
from app.modules.users.dependencies import get_current_token_data
from app.modules.users.schemas import UpdateProfileRequest
from app.modules.users.service import user_to_response

logger = get_logger(__name__)
auth_router = APIRouter(prefix="/auth", tags=["authentication"])
me_router = APIRouter(prefix="/user", tags=["user"])


@auth_router.post("/register")
async def register(
    register_request: RegisterRequest,
    request: Request,
    db: DbSession,
) -> JSONResponse:
    """Register a new user account."""
    start_time = datetime.now()
    try:
        user, err = await register_user(
            db=db,
            username=register_request.username,
            email=register_request.email,
            password=register_request.password,
            display_name=register_request.display_name,
            request=request,
        )
        if user is None:
            return error_response(
                message=err or "Registration failed",
                status_code=status.HTTP_409_CONFLICT,
                error_code=ErrorCode.CONFLICT,
                request=request,
                start_time=start_time,
            )

        logger.info("auth.user_registered", user_id=str(user.id))
        return created_response(
            data={
                "message": "User registered successfully. Please check your email to verify your account.",
                "user": user_to_response(user).model_dump(mode="json"),
            },
            request=request,
            start_time=start_time,
        )
    except Exception as e:
        log_unexpected_error(logger, "auth.register_failed", e)
        return error_response(
            message="Registration failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post("/login")
async def login(
    request: Request,
    db: DbSession,
    user: User = Depends(authenticate_form_user),
) -> JSONResponse:
    """
    Authenticate a user via OAuth2 password form.
    Returns the standard API success envelope with token information in `data`.
    The refresh token is set as an httpOnly secure cookie.
    """
    start_time = datetime.now()
    try:
        access_token, raw_refresh = await create_session_tokens(db, user, request)
        user_resp = user_to_response(user)

        response = success_response(
            data={
                "access_token": access_token,
                "token_type": "bearer",
                "user": user_resp.model_dump(mode="json"),
            },
            request=request,
            start_time=start_time,
            status_code=200,
            top_level_fields={
                "access_token": access_token,
                "token_type": "bearer",
            },
        )

        logger.info(
            "auth.user_logged_in",
        )
        return add_refresh_token_to_cookie(response, raw_refresh)

    except Exception as e:
        log_unexpected_error(logger, "auth.login_failed", e)
        return error_response(
            message="Login failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post("/logout")
async def logout(
    request: Request,
    db: DbSession,
    raw_token: str = Depends(get_refresh_token_from_cookie),
) -> JSONResponse:
    """Revoke the current refresh token (read from cookie), logging the user out."""
    start_time = datetime.now()
    try:
        ok, err = await logout_session(db, raw_token, request)
        if not ok:
            return error_response(
                message=err or "Logout failed",
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code=ErrorCode.BAD_REQUEST,
                request=request,
                start_time=start_time,
            )

        response = success_response(
            data={"message": "Logged out successfully"},
            request=request,
            start_time=start_time,
        )
        logger.info("auth.user_logged_out")
        return delete_refresh_token_cookie(response)

    except Exception as e:
        log_unexpected_error(logger, "auth.logout_failed", e)
        return error_response(
            message="Logout failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post(
    "/logout-all",
    status_code=status.HTTP_200_OK,
    summary="Log out on all devices",
    responses={
        200: {"model": Response200, "description": "All sessions revoked"},
        401: {"model": Response401, "description": "Missing or invalid token"},
        500: {"model": Response500, "description": "Unexpected error"},
    },
)
async def logout_all(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
) -> JSONResponse:
    """
    Revoke every refresh-token session of the authenticated user (all devices)
    and clear the refresh-token cookie. Access tokens already issued stay valid
    until they expire (`ACCESS_TOKEN_EXPIRE_MINUTES`).
    """
    start_time = datetime.now()
    try:
        await logout_all_sessions(db, current_user.id, request)
        response = success_response(
            data={"message": "Logged out from all sessions"},
            request=request,
            start_time=start_time,
        )
        return delete_refresh_token_cookie(response)
    except Exception as e:
        log_unexpected_error(logger, "auth.logout_all_failed", e)
        return error_response(
            message="Logout failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post("/refresh")
async def refresh_tokens(
    request: Request,
    db: DbSession,
    raw_token: str = Depends(get_refresh_token_from_cookie),
) -> JSONResponse:
    """
    Issue a new access + refresh token pair using the refresh token cookie.
    The old refresh token is revoked (rotation). No access token required.
    """
    start_time = datetime.now()
    try:
        access_token, new_raw_refresh, err = await refresh_session(
            db, raw_token, request
        )
        if access_token is None or new_raw_refresh is None:
            return error_response(
                message=err or "Token refresh failed",
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_code=ErrorCode.UNAUTHORIZED,
                request=request,
                start_time=start_time,
            )

        response = success_response(
            data={
                "access_token": access_token,
                "token_type": "bearer",
            },
            request=request,
            start_time=start_time,
            status_code=200,
            top_level_fields={
                "access_token": access_token,
                "token_type": "bearer",
            },
        )
        logger.info("auth.token_refreshed")
        return add_refresh_token_to_cookie(response, new_raw_refresh)

    except Exception as e:
        log_unexpected_error(logger, "auth.refresh_failed", e)
        return error_response(
            message="Token refresh failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post("/password/forgot")
async def password_forgot(
    body: PasswordForgotRequest,
    request: Request,
    db: DbSession,
) -> JSONResponse:
    """Initiate a password-reset email."""
    start_time = datetime.now()
    try:
        await forgot_password(db, body.email, request)
        # Always return success to prevent email enumeration
        return ok_response(
            data={
                "message": "If the email exists, a password reset link has been sent."
            },
            request=request,
            start_time=start_time,
        )
    except Exception as e:
        log_unexpected_error(logger, "auth.password_forgot_failed", e)
        return error_response(
            message="Failed to process password reset request",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post("/password/reset")
async def password_reset(
    body: PasswordResetRequest,
    request: Request,
    db: DbSession,
) -> JSONResponse:
    """Complete a password reset using the token from the email link."""
    start_time = datetime.now()
    try:
        ok, err = await reset_password(db, body.token, body.new_password, request)
        if not ok:
            return error_response(
                message=err or "Password reset failed",
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code=ErrorCode.BAD_REQUEST,
                request=request,
                start_time=start_time,
            )
        logger.info("auth.password_reset_completed")
        return ok_response(
            data={"message": "Password has been reset successfully"},
            request=request,
            start_time=start_time,
        )
    except Exception as e:
        log_unexpected_error(logger, "auth.password_reset_failed", e)
        return error_response(
            message="Password reset failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post("/email/verify")
async def email_verify(
    body: EmailVerifyRequest,
    request: Request,
    db: DbSession,
) -> JSONResponse:
    """Submit the token from the verification email link."""
    start_time = datetime.now()
    try:
        ok, err = await verify_email(db, body.token, request)
        if not ok:
            return error_response(
                message=err or "Email verification failed",
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code=ErrorCode.BAD_REQUEST,
                request=request,
                start_time=start_time,
            )
        logger.info("auth.email_verified")
        return ok_response(
            data={"message": "Email verified successfully"},
            request=request,
            start_time=start_time,
        )
    except Exception as e:
        log_unexpected_error(logger, "auth.email_verify_failed", e)
        return error_response(
            message="Email verification failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post("/email/verify/resend")
async def email_verify_resend(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
) -> JSONResponse:
    """Request a new verification email (requires authentication)."""
    start_time = datetime.now()
    try:
        ok, err = await resend_verification_email(db, current_user.id, request)
        if not ok:
            return error_response(
                message=err or "Failed to resend verification email",
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code=ErrorCode.BAD_REQUEST,
                request=request,
                start_time=start_time,
            )
        logger.info("auth.verification_email_resent", user_id=str(current_user.id))
        return ok_response(
            data={"message": "Verification email has been resent"},
            request=request,
            start_time=start_time,
        )
    except Exception as e:
        log_unexpected_error(logger, "auth.resend_verification_failed", e)
        return error_response(
            message="Failed to resend verification email",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@auth_router.post("/2fa/enable")
async def enable_2fa(request: Request) -> JSONResponse:
    start_time = datetime.now()
    return error_response(
        message="Two-factor authentication is not yet implemented",
        status_code=501,
        error_code=ErrorCode.INTERNAL_ERROR,
        request=request,
        start_time=start_time,
    )


@auth_router.post("/2fa/disable")
async def disable_2fa(request: Request) -> JSONResponse:
    start_time = datetime.now()
    return error_response(
        message="Two-factor authentication is not yet implemented",
        status_code=501,
        error_code=ErrorCode.INTERNAL_ERROR,
        request=request,
        start_time=start_time,
    )


@auth_router.post("/2fa/verify")
async def verify_2fa(request: Request) -> JSONResponse:
    start_time = datetime.now()
    return error_response(
        message="Two-factor authentication is not yet implemented",
        status_code=501,
        error_code=ErrorCode.INTERNAL_ERROR,
        request=request,
        start_time=start_time,
    )


@me_router.patch("/me")
async def update_me(
    body: UpdateProfileRequest,
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
) -> JSONResponse:
    """Update the current user's profile."""
    start_time = datetime.now()
    try:
        user, err = await update_profile(
            db=db,
            user_id=current_user.id,
            display_name=body.display_name,
            new_email=body.email,
            request=request,
        )
        if user is None:
            return error_response(
                message=err or "Profile update failed",
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code=ErrorCode.BAD_REQUEST,
                request=request,
                start_time=start_time,
            )
        return ok_response(
            data={"user": user_to_response(user).model_dump(mode="json")},
            request=request,
            start_time=start_time,
        )
    except Exception as e:
        log_unexpected_error(logger, "user.profile_update_failed", e)
        return error_response(
            message="Profile update failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@me_router.patch("/me/password")
async def change_my_password(
    body: ChangePasswordRequest,
    request: Request,
    db: DbSession,
    token_data: Annotated[TokenData, Depends(get_current_token_data)],
    current_user: CurrentUser,
) -> JSONResponse:
    """
    Change the current user's password (requires current password).
    Every other session of the user is logged out; the calling session stays.
    """
    start_time = datetime.now()
    try:
        ok, err = await change_password(
            db=db,
            user_id=current_user.id,
            current_password=body.current_password,
            new_password=body.new_password,
            request=request,
            keep_session_family=(
                uuid.UUID(token_data.session_id) if token_data.session_id else None
            ),
        )
        if not ok:
            return error_response(
                message=err or "Password change failed",
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code=ErrorCode.BAD_REQUEST,
                request=request,
                start_time=start_time,
            )
        return ok_response(
            data={"message": "Password changed successfully"},
            request=request,
            start_time=start_time,
        )
    except Exception as e:
        log_unexpected_error(logger, "user.password_change_failed", e)
        return error_response(
            message="Password change failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


router = APIRouter()
router.include_router(auth_router)
router.include_router(me_router)
