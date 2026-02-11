# src/api/auth/router.py

"""
Authentication routes for token generation and refresh.
"""

from datetime import datetime
from typing import Any, Dict
from fastapi import APIRouter, Depends, Request, status

from src.api.auth.models import (
    TokenPair, TokenRequest, RefreshTokenRequest
)
from src.api.auth.jwt_utils import (
    create_access_token, create_refresh_token, validate_refresh_token, revoke_refresh_token
)
from src.common.models import ErrorCode
from src.common.responses import ok_response, error_response
from src.common.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()


@router.post("/token", response_model=Dict[str, Any])
async def get_token(
    token_request: TokenRequest,
    request: Request
) -> Dict[str, Any]:
    """
    Generate JWT access token and opaque refresh token.
    """
    start_time = datetime.now()
    
    try:
        """"
        if token_request.username not in VALID_USERNAMES:
            return error_response(
                message="Username not found",
                status_code=status.HTTP_404_NOT_FOUND,
                request=request,
                start_time=start_time
            )
        """
        user_id = str(hash(token_request.username) % 10000)
        
        access_token = create_access_token(token_request.username, user_id)
        refresh_token = create_refresh_token(token_request.username, user_id)
        
        logger.info(f"Tokens generated for user: {token_request.username}")
        
        return ok_response(
            data={
                "user": {
                    "id": user_id,
                    "username": token_request.username
                },
                "tokens": TokenPair(
                    access_token=access_token,
                    refresh_token=refresh_token
                )
            },
            message="Tokens generated successfully",
            request=request,
            start_time=start_time
        )
        
    except Exception as e:
        logger.error(f"Error during token generation: {e}")
        return error_response(
            message="Token generation failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time
        )


@router.post("/refresh", response_model=Dict[str, Any])
async def refresh_tokens(
    refresh_request: RefreshTokenRequest,
    request: Request
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
                start_time=start_time
            )
        
        # Revoke the old refresh token
        revoke_refresh_token(refresh_request.refresh_token)
        
        # Create new tokens
        new_access_token = create_access_token(refresh_token_obj.username, refresh_token_obj.user_id)
        new_refresh_token = create_refresh_token(refresh_token_obj.username, refresh_token_obj.user_id)
        
        logger.info(f"Tokens refreshed for user: {refresh_token_obj.username}")
        
        return ok_response(
            data={
                "tokens": TokenPair(
                    access_token=new_access_token,
                    refresh_token=new_refresh_token
                )
            },
            message="Tokens refreshed successfully",
            request=request,
            start_time=start_time
        )
        
    except Exception as e:
        logger.error(f"Error during token refresh: {e}")
        return error_response(
            message="Token refresh failed",
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            error_code=ErrorCode.INTERNAL_ERROR,
            request=request,
            start_time=start_time
        )