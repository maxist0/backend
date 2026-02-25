# src/api/auth/crud.py

"""
Data-access layer for auth.
User records are kept in-memory (swap for a real DB later).
Refresh tokens are stored in Redis with automatic TTL-based expiry.
"""

from datetime import datetime
from typing import Dict, Optional

import redis.asyncio as redis

from src.api.auth.models import RefreshToken, UserRecord
from src.common.logging import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------

_RT_PREFIX = "rt:"


def _rt_key(token: str) -> str:
    return f"{_RT_PREFIX}{token}"


# ---------------------------------------------------------------------------
# In-memory user store (replace with DB later)
# ---------------------------------------------------------------------------

user_db: Dict[str, UserRecord] = {}


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
# Refresh-token CRUD (Redis-backed, async)
# ---------------------------------------------------------------------------


async def save_refresh_token(rt: RefreshToken, redis_client: redis.Redis) -> None:
    """Persist a refresh token in Redis with a TTL derived from its expiry."""
    ttl = int((rt.expires_at - datetime.utcnow()).total_seconds())
    if ttl <= 0:
        logger.warning(f"Refresh token already expired, not saving: {rt.id}")
        return
    await redis_client.set(_rt_key(rt.token), rt.model_dump_json(), ex=ttl)
    logger.debug(f"Saved refresh token {rt.id} with TTL={ttl}s")


async def get_active_refresh_token(
    token: str, redis_client: redis.Redis
) -> Optional[RefreshToken]:
    """Return the RefreshToken stored in Redis, or None if missing/expired."""
    data = await redis_client.get(_rt_key(token))
    if data is None:
        return None
    return RefreshToken.model_validate_json(data)


async def revoke_refresh_token(token: str, redis_client: redis.Redis) -> bool:
    """Delete the refresh token from Redis. Returns True if it existed."""
    deleted = await redis_client.delete(_rt_key(token))
    return deleted > 0
