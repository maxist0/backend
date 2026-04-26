# src/api/user/service.py

"""
Business logic for user profile operations.
Delegates to auth service for shared operations.
"""

from datetime import timedelta
import secrets
from typing import Optional, Tuple
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Request
from src.api.user.models import hash_token, utcnow
from src.api.user.models import User, EmailToken, EmailTokenPurpose
from src.api.user import crud
from src.utils.email_tasks import send_verification_email_task

from src.api.auth.service import (
    change_password,
    user_to_response,
)

__all__ = ["change_password", "user_to_response"]


# ---------------------------------------------------------------------------
# Profile update
# ---------------------------------------------------------------------------


async def update_profile(
    db: AsyncSession,
    user_id: uuid.UUID,
    display_name: Optional[str] = None,
    new_email: Optional[str] = None,
    request: Optional[Request] = None,
) -> Tuple[Optional[User], Optional[str]]:
    """
    Update user profile fields.
    If email is changed, a confirmation token is created (re-verification required).
    """
    user = await crud.get_user_by_id(db, user_id)
    if user is None:
        return None, "User not found"

    if display_name is not None:
        user.display_name = display_name
        
    if new_email is not None:
        new_email = new_email.lower() 

    if new_email is not None and new_email != user.email:
        # Check if new email is already taken
        existing = await crud.get_user_by_email(db, new_email)
        if existing and existing.id != user.id:
            return None, "Email already in use"

        # Create a change-email token
        await crud.invalidate_tokens_for_user_purpose(
            db, user.id, EmailTokenPurpose.CHANGE_EMAIL
        )
        raw_token = secrets.token_urlsafe(32)
        email_token = EmailToken(
            user_id=user.id,
            purpose=EmailTokenPurpose.CHANGE_EMAIL,
            token_hash=hash_token(raw_token),
            new_email=new_email,
            expires_at=utcnow() + timedelta(hours=24),
        )
        await crud.create_email_token(db, email_token)
        send_verification_email_task.delay(new_email, user.username, raw_token)

    await crud.update_user(db, user)
    return user, None
