# src/api/user/crud.py

"""
Data-access layer for user profile operations.
Delegates to auth CRUD for shared operations.
"""

from src.api.auth.crud import (
    get_user_by_id,
    get_user_by_email,
    update_user,
    get_password_credential,
    update_password_hash,
    invalidate_tokens_for_user_purpose,
    create_email_token,
)

__all__ = [
    "get_user_by_id",
    "get_user_by_email",
    "update_user",
    "get_password_credential",
    "update_password_hash",
    "invalidate_tokens_for_user_purpose",
    "create_email_token",
]
