# src/api/user/router.py

"""
User profile routes: GET /me, PATCH /me, PATCH /me/password.
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse

from src.api.auth.schemas import TokenData
from src.api.user.dependencies import get_current_token_data, get_current_user
from src.api.user.schemas import ChangePasswordRequest, UpdateProfileRequest
from src.api.user.models import User
from src.api.user.service import change_password, update_profile, user_to_response
from src.common.dependencies import DbSession
from src.common.logging import get_logger, log_unexpected_error
from src.common.schemas import ErrorCode
from src.common.responses import error_response, ok_response

logger = get_logger(__name__)
router = APIRouter()


@router.get("/me")
async def get_me(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> JSONResponse:
    """Return the currently authenticated user's profile."""
    start_time = datetime.now()
    return ok_response(
        data={"user": user_to_response(current_user).model_dump(mode="json")},
        request=request,
        start_time=start_time,
    )


@router.patch("/me")
async def update_me(
    body: UpdateProfileRequest,
    request: Request,
    db: DbSession,
    current_user: User = Depends(get_current_user),
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


@router.patch("/me/password")
async def change_my_password(
    body: ChangePasswordRequest,
    request: Request,
    db: DbSession,
    token_data: Annotated[TokenData, Depends(get_current_token_data)],
    current_user: User = Depends(get_current_user),
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
