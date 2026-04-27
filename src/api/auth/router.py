# src/api/auth/router.py

"""
Authentication routes:
  POST /register, /login, /refresh, /logout
  POST /password/forgot, /password/reset
  POST /email/verify, /email/verify/resend
  POST /2fa/enable, /2fa/disable, /2fa/verify  (stubs)
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from src.api.auth.dependencies import (
    authenticate_form_user,
    get_current_user,
    get_refresh_token_from_cookie,
)
from src.api.auth.schemas import (
    EmailVerifyRequest,
    PasswordForgotRequest,
    PasswordResetRequest,
    RegisterRequest,
)
from src.api.auth.models import User
from src.api.auth.service import (
    add_refresh_token_to_cookie,
    create_session_tokens,
    delete_refresh_token_cookie,
    forgot_password,
    logout_session,
    refresh_session,
    register_user,
    resend_verification_email,
    reset_password,
    user_to_response,
    verify_email,
)
from src.common.dependencies import DbSession
from src.common.logging import get_logger
from src.common.schemas import ErrorCode
from src.common.responses import (
    created_response,
    error_response,
    ok_response,
    success_response,
)

logger = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@router.post("/register")
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

        logger.info(
            "auth.user_registered",
            user_id=str(user.id),
            username=user.username,
        )
        return created_response(
            data={
                "message": "User registered successfully. Please check your email to verify your account.",
                "user": user_to_response(user).model_dump(mode="json"),
            },
            request=request,
            start_time=start_time,
        )
    except Exception as e:
        logger.error("auth.register_failed", error=str(e), exc_info=True)
        return error_response(
            message="Registration failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.post("/login")
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
        redis_client = request.app.state.cache
        access_token, raw_refresh = await create_session_tokens(
            db, user, redis_client, request
        )
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
        logger.error("auth.login_failed", error=str(e), exc_info=True)
        return error_response(
            message="Login failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post("/logout")
async def logout(
    request: Request,
    db: DbSession,
    raw_token: str = Depends(get_refresh_token_from_cookie),
) -> JSONResponse:
    """Revoke the current refresh token (read from cookie), logging the user out."""
    start_time = datetime.now()
    try:
        redis_client = request.app.state.cache
        ok, err = await logout_session(db, raw_token, redis_client, request)
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
        logger.error("auth.logout_failed", error=str(e), exc_info=True)
        return error_response(
            message="Logout failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


@router.post("/refresh")
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
        redis_client = request.app.state.cache
        access_token, new_raw_refresh, err = await refresh_session(
            db, raw_token, redis_client, request
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
        logger.error("auth.refresh_failed", error=str(e), exc_info=True)
        return error_response(
            message="Token refresh failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


# ---------------------------------------------------------------------------
# Password forgot / reset
# ---------------------------------------------------------------------------


@router.post("/password/forgot")
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
        logger.error("auth.password_forgot_failed", error=str(e), exc_info=True)
        return error_response(
            message="Failed to process password reset request",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@router.post("/password/reset")
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
        logger.error("auth.password_reset_failed", error=str(e), exc_info=True)
        return error_response(
            message="Password reset failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


@router.post("/email/verify")
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
        logger.error("auth.email_verify_failed", error=str(e), exc_info=True)
        return error_response(
            message="Email verification failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@router.post("/email/verify/resend")
async def email_verify_resend(
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_current_user),
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
        logger.error("auth.resend_verification_failed", error=str(e), exc_info=True)
        return error_response(
            message="Failed to resend verification email",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


# ---------------------------------------------------------------------------
# 2FA stubs (not yet implemented)
# ---------------------------------------------------------------------------


@router.post("/2fa/enable")
async def enable_2fa(request: Request) -> JSONResponse:
    start_time = datetime.now()
    return error_response(
        message="Two-factor authentication is not yet implemented",
        status_code=501,
        error_code=ErrorCode.INTERNAL_ERROR,
        request=request,
        start_time=start_time,
    )


@router.post("/2fa/disable")
async def disable_2fa(request: Request) -> JSONResponse:
    start_time = datetime.now()
    return error_response(
        message="Two-factor authentication is not yet implemented",
        status_code=501,
        error_code=ErrorCode.INTERNAL_ERROR,
        request=request,
        start_time=start_time,
    )


@router.post("/2fa/verify")
async def verify_2fa(request: Request) -> JSONResponse:
    start_time = datetime.now()
    return error_response(
        message="Two-factor authentication is not yet implemented",
        status_code=501,
        error_code=ErrorCode.INTERNAL_ERROR,
        request=request,
        start_time=start_time,
    )
