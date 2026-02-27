# src/api/user/service.py

"""
Business logic for user profile operations.
Delegates to auth service for shared operations.
"""

from src.api.auth.service import (
    change_password,
    update_profile,
    user_to_response,
)

__all__ = ["change_password", "update_profile", "user_to_response"]
