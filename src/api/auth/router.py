# src/api/auth/router.py

"""
Authentication routes for token generation and refresh.
"""

from datetime import datetime
from typing import Any, Dict
from fastapi import APIRouter, Request, status, Depends
from fastapi.responses import JSONResponse


from src.api.auth.models import (
    RegisterRequest,
    UserRecord,
    RefreshToken,
)
from src.api.auth.jwt_utils import (
    create_access_token,
    create_refresh_token,
    revoke_refresh_token,
)
from src.api.auth.service import register_user, add_refresh_token_to_cookie,delete_refresh_token_cookie
from src.common.models import ErrorCode
from src.common.responses import ok_response, error_response, success_response
from src.common.logging import get_logger
from src.api.auth.dependencies import authenticate_form_user, get_current_refresh_token_data, get_current_user

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


@router.get("/me", response_model=Dict[str, Any])
async def me(
    request: Request,
    current_user: dict = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    Return the currently authenticated user's profile.
    Requires a valid Bearer access token.
    """
    start_time = datetime.now()
    return ok_response(
        data={"user": current_user},
        request=request,
        start_time=start_time,
    )


@router.post("/login", response_model=Dict[str, Any])
async def login(
    request: Request,
    user: UserRecord = Depends(authenticate_form_user),
) -> Dict[str, Any]:
    """
    Authenticate a user via OAuth2 password form.
    Returns an OAuth2-compatible response with the access token at the top level
    (required for Swagger / OpenAPI "Authorize" to capture it automatically).
    The refresh token is set as an httpOnly secure cookie.
    """
    start_time = datetime.now()

    try:
        access_token = create_access_token(user.username, user.id)
        refresh_token = create_refresh_token(user.username, user.id)

        logger.info(f"User logged in: {user.username}")

        # Return standard OAuth2 token response so Swagger captures access_token
        # automatically and uses it as Authorization: Bearer <token>.
        response = JSONResponse(
            content={
                "access_token": access_token,
                "token_type": "bearer",
                "user": {"id": user.id, "username": user.username},
            },
            status_code=200,
        )
        response = add_refresh_token_to_cookie(response, refresh_token)
        return response

    except Exception as e:
        logger.error(f"Error during login: {e}")
        return error_response(
            message="Login failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@router.post("/logout")
async def logout(
    request: Request,
    refresh_token_data: RefreshToken = Depends(get_current_refresh_token_data),
) -> Dict[str, Any]:
    """
    Revoke the current refresh token (read from cookie), effectively logging the user out.
    """
    start_time = datetime.now()

    try:
        revoked = revoke_refresh_token(refresh_token_data.token)
        if not revoked:
            return error_response(
                message="Refresh token not found or already revoked",
                status_code=status.HTTP_400_BAD_REQUEST,
                error_code=ErrorCode.CONFLICT,
                request=request,
                start_time=start_time,
            )

        logger.info("User logged out - refresh token revoked")

        # Clear the refresh token cookie
        response_data = success_response(
            data={"message": "Logged out successfully"},
            request=request,
            start_time=start_time,
        )
        response_data = delete_refresh_token_cookie(response_data)
        return response_data

    except Exception as e:
        logger.error(f"Error during logout: {e}")
        return error_response(
            message="Logout failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )


@router.post("/refresh")
async def refresh_tokens(
    request: Request,
    refresh_token_data: RefreshToken = Depends(get_current_refresh_token_data),
) -> Dict[str, Any]:
    """
    Issue a new access + refresh token pair using the refresh token from the cookie.
    The old refresh token is revoked (rotation). No access token required.
    """
    start_time = datetime.now()

    try:
        # Revoke the old refresh token (rotation)
        revoke_refresh_token(refresh_token_data.token)

        # Create new tokens
        new_access_token = create_access_token(
            refresh_token_data.username, refresh_token_data.user_id
        )
        new_refresh_token = create_refresh_token(
            refresh_token_data.username, refresh_token_data.user_id
        )

        logger.info(f"Tokens refreshed for user: {refresh_token_data.username}")

        # Return new access token in body; set new refresh token cookie
        response_data = JSONResponse(
            content={
                "access_token": new_access_token,
                "token_type": "bearer",
            },
            status_code=200,
        )
        response_data = add_refresh_token_to_cookie(response_data, new_refresh_token)
        return response_data

    except Exception as e:
        logger.error(f"Error during token refresh: {e}")
        return error_response(
            message="Token refresh failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time,
        )
