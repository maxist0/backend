# src/api/auth/service.py

"""
Business logic for authentication.
Handles password hashing (argon2) and user registration / authentication.
"""

from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, VerificationError, InvalidHashError

from src.api.auth.models import UserRecord
from src.api.auth import crud
from src.common.logging import get_logger

logger = get_logger(__name__)

_ph = PasswordHasher()


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return an argon2 hash of *password*."""
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return True if *password* matches *password_hash*."""
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# User business logic
# ---------------------------------------------------------------------------


def register_user(username: str, password: str) -> Optional[UserRecord]:
    """
    Register a new user.
    Returns the created UserRecord, or None if the username is already taken.
    """
    if crud.get_user_by_username(username) is not None:
        return None

    user = UserRecord(username=username, password_hash=hash_password(password))
    crud.create_user(user)
    logger.info(f"User registered: {username} (id={user.id})")
    return user


def authenticate_user(username: str, password: str) -> Optional[UserRecord]:
    """
    Verify credentials.
    Returns the UserRecord on success, None if credentials are invalid.
    """
    print(f"Authenticating user: {username}")
    print(f"password: {password}")
    user = crud.get_user_by_username(username)
    if user is None or not verify_password(password, user.password_hash):
        return None
    return user


def add_refresh_token_to_cookie(response, refresh_token: str):
    """Helper to set the refresh token cookie with secure settings."""
    response.set_cookie(
            key="refresh_token",
            value=refresh_token,
            httponly=True,
            samesite="lax",
            path="/auth", 
        )
    return response

def delete_refresh_token_cookie(response):
    """Helper to delete the refresh token cookie."""
    response.delete_cookie(key="refresh_token", path="/auth")
    return response