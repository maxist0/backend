# src/api/auth/crud.py

"""
Data-access layer for auth.
All in-memory stores live here; swap these functions for real DB calls later.
"""

from datetime import datetime
from typing import Dict, List, Optional

from src.api.auth.models import RefreshToken, UserRecord
from src.common.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# In-memory stores (replace with DB / Redis later)
# ---------------------------------------------------------------------------

user_db: Dict[str, UserRecord] = {}
refresh_token_db: List[RefreshToken] = []


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


def get_user_by_username(username: str) -> Optional[UserRecord]:
    """Return the user with *username*, or None if not found."""
    return user_db.get(username)


def create_user(user: UserRecord) -> UserRecord:
    """Persist a new user record and return it."""
    user_db[user.username] = user
    return user


# ---------------------------------------------------------------------------
# Refresh-token CRUD
# ---------------------------------------------------------------------------


def save_refresh_token(rt: RefreshToken) -> None:
    """Persist a new refresh token."""
    refresh_token_db.append(rt)


def get_active_refresh_token(token: str) -> Optional[RefreshToken]:
    """Return a non-revoked, non-expired refresh token, or None."""
    for rt in refresh_token_db:
        if rt.token == token and not rt.revoked and rt.expires_at > datetime.utcnow():
            return rt
    return None


def revoke_refresh_token(token: str) -> bool:
    """Mark a refresh token as revoked. Returns True if found."""
    for rt in refresh_token_db:
        if rt.token == token:
            rt.revoked = True
            return True
    return False
