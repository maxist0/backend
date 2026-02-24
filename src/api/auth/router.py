# src/api/auth/router.py

"""
Authentication routes for token generation and refresh.
"""

from datetime import datetime
from typing import Any, Dict
from fastapi import APIRouter, Request, status

from src.api.auth.models import TokenPair, RefreshTokenRequest, RegisterRequest, LoginRequest, LogoutRequest
from src.api.auth.jwt_utils import (
    create_access_token,
    create_refresh_token,
    validate_refresh_token,
    revoke_refresh_token,
)
from src.api.auth.service import register_user, authenticate_user
from src.common.models import ErrorCode
from src.common.responses import ok_response, error_response
from src.common.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.post("/register", response_model=Dict[str, Any])
async def register(
    register_request: RegisterRequest, request: Request
) -> Dict[str, Any]:
    """
    Register a new user account.
    """
    start_time = datetime.now()

    try:
        user = register_user(register_request.username, register_request.password)
        if user is None:
            return error_response(
                message="Username already taken",
                status_code=status.HTTP_409_CONFLICT,
                request=request,
                start_time=start_time,
            )

        logger.info(f"User registered: {register_request.username}")

        return ok_response(
            data={"user": {"id": user.id, "username": user.username}},
            message="User registered successfully",
            request=request,
            start_time=start_time,
        )

    except Exception as e:
        logger.error(f"Error during registration: {e}")
        return error_response(
            message="Registration failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@router.post("/login", response_model=Dict[str, Any])
async def login(
    login_request: LoginRequest, request: Request
) -> Dict[str, Any]:
    """
    Authenticate a user and return an access + refresh token pair.
    """
    start_time = datetime.now()

    try:
        user = authenticate_user(login_request.username, login_request.password)
        if user is None:
            return error_response(
                message="Invalid username or password",
                status_code=status.HTTP_401_UNAUTHORIZED,
                error_code=ErrorCode.UNAUTHORIZED,
                request=request,
                start_time=start_time,
            )

        access_token = create_access_token(user.username, user.id)
        refresh_token = create_refresh_token(user.username, user.id)

        logger.info(f"User logged in: {user.username}")

        return ok_response(
            data={
                "user": {"id": user.id, "username": user.username},
                "tokens": TokenPair(
                    access_token=access_token, refresh_token=refresh_token
                ),
            },
            message="Login successful",
            request=request,
            start_time=start_time,
        )

    except Exception as e:
        logger.error(f"Error during login: {e}")
        return error_response(
            message="Login failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@router.post("/logout", response_model=Dict[str, Any])
async def logout(
    logout_request: LogoutRequest, request: Request
) -> Dict[str, Any]:
    """
    Revoke the provided refresh token, effectively logging the user out.
    """
    start_time = datetime.now()

    try:
        revoked = revoke_refresh_token(logout_request.refresh_token)
        if not revoked:
            return error_response(
                message="Refresh token not found or already revoked",
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code=ErrorCode.CONFLICT,
                request=request,
                start_time=start_time,
            )

        logger.info("User logged out – refresh token revoked")

        return ok_response(
            data={},
            message="Logged out successfully",
            request=request,
            start_time=start_time,
        )

    except Exception as e:
        logger.error(f"Error during logout: {e}")
        return error_response(
            message="Logout failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@router.post("/refresh", response_model=Dict[str, Any])
async def refresh_tokens(
    refresh_request: RefreshTokenRequest, request: Request
) -> Dict[str, Any]:
    """
    Refresh access and refresh tokens using an opaque refresh token.
    """
    start_time = datetime.now()

    try:
        # Validate the opaque refresh token
        refresh_token_obj = validate_refresh_token(refresh_request.refresh_token)
        if not refresh_token_obj:
            return error_response(
                message="Invalid or expired refresh token",
                status_code=status.HTTP_401_UNAUTHORIZED,
                request=request,
                start_time=start_time,
            )

        # Revoke the old refresh token
        revoke_refresh_token(refresh_request.refresh_token)

        # Create new tokens
        new_access_token = create_access_token(
            refresh_token_obj.username, refresh_token_obj.user_id
        )
        new_refresh_token = create_refresh_token(
            refresh_token_obj.username, refresh_token_obj.user_id
        )

        logger.info(f"Tokens refreshed for user: {refresh_token_obj.username}")

        return ok_response(
            data={
                "tokens": TokenPair(
                    access_token=new_access_token, refresh_token=new_refresh_token
                )
            },
            message="Tokens refreshed successfully",
            request=request,
            start_time=start_time,
        )

    except Exception as e:
        logger.error(f"Error during token refresh: {e}")
        return error_response(
            message="Token refresh failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )
