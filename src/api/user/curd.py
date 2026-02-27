# src/api/user/curd.py

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
)

__all__ = [
    "get_user_by_id",
    "get_user_by_email",
    "update_user",
    "get_password_credential",
    "update_password_hash",
]
