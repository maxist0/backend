# src/api/auth/crud.py

"""
Data-access layer for authentication.
All database operations go through this module.
Refresh-token cache operations use Redis.
"""

import uuid
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as redis
from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.models import (
    AuthAuditLog,
    EmailToken,
    EmailTokenPurpose,
    PasswordCredential,
    RefreshTokenCache,
    Session,
    User,
    utcnow,
)
from src.common.constants import REDIS_REFRESH_TOKEN_PREFIX
from src.common.logging import get_logger
from src.common.metrics import DatabaseTimer

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Redis key helpers
# ---------------------------------------------------------------------------


def _rt_key(raw_token: str) -> str:
    return f"{REDIS_REFRESH_TOKEN_PREFIX}{raw_token}"


# ---------------------------------------------------------------------------
# User CRUD
# ---------------------------------------------------------------------------


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> Optional[User]:
    with DatabaseTimer("select", "users"):
        result = await db.execute(select(User).where(User.id == user_id))
        return result.scalars().first()


async def get_user_by_username(db: AsyncSession, username: str) -> Optional[User]:
    with DatabaseTimer("select", "users"):
        result = await db.execute(select(User).where(User.username == username))
        return result.scalars().first()


async def get_user_by_email(db: AsyncSession, email: str) -> Optional[User]:
    with DatabaseTimer("select", "users"):
        result = await db.execute(select(User).where(User.email == email))
        return result.scalars().first()


async def create_user(db: AsyncSession, user: User) -> User:
    with DatabaseTimer("insert", "users"):
        db.add(user)
        await db.flush()
    return user


async def update_user(db: AsyncSession, user: User) -> User:
    with DatabaseTimer("update", "users"):
        user.updated_at = utcnow()
        await db.flush()
    return user


# ---------------------------------------------------------------------------
# PasswordCredential CRUD
# ---------------------------------------------------------------------------


async def create_password_credential(
    db: AsyncSession, credential: PasswordCredential
) -> PasswordCredential:
    with DatabaseTimer("insert", "password_credentials"):
        db.add(credential)
        await db.flush()
    return credential


async def get_password_credential(
    db: AsyncSession, user_id: uuid.UUID
) -> Optional[PasswordCredential]:
    with DatabaseTimer("select", "password_credentials"):
        result = await db.execute(
            select(PasswordCredential).where(PasswordCredential.user_id == user_id)
        )
        return result.scalars().first()


async def update_password_hash(
    db: AsyncSession, user_id: uuid.UUID, new_hash: str
) -> None:
    with DatabaseTimer("update", "password_credentials"):
        await db.execute(
            update(PasswordCredential)
            .where(PasswordCredential.user_id == user_id)
            .values(password_hash=new_hash, updated_at=utcnow())
        )
        await db.flush()


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


async def create_session(db: AsyncSession, session: Session) -> Session:
    with DatabaseTimer("insert", "sessions"):
        db.add(session)
        await db.flush()
    return session


async def get_session_by_token_hash(
    db: AsyncSession, token_hash: str
) -> Optional[Session]:
    with DatabaseTimer("select", "sessions"):
        result = await db.execute(
            select(Session).where(Session.token_hash == token_hash)
        )
        return result.scalars().first()


async def revoke_session(
    db: AsyncSession, session_id: uuid.UUID, reason: str = "logout"
) -> None:
    with DatabaseTimer("update", "sessions"):
        await db.execute(
            update(Session)
            .where(Session.id == session_id)
            .values(is_revoked=True, revoke_reason=reason, revoked_at=utcnow())
        )
        await db.flush()


async def revoke_family(
    db: AsyncSession, family_id: uuid.UUID, reason: str = "reuse_detected"
) -> list[Session]:
    """Revoke all active sessions in a rotation family. Returns affected sessions."""
    with DatabaseTimer("update", "sessions"):
        result = await db.execute(
            select(Session).where(
                and_(Session.family_id == family_id, Session.is_revoked == False)  # noqa: E712
            )
        )
        sessions = list(result.scalars().all())
        if sessions:
            await db.execute(
                update(Session)
                .where(
                    and_(Session.family_id == family_id, Session.is_revoked == False)  # noqa: E712
                )
                .values(is_revoked=True, revoke_reason=reason, revoked_at=utcnow())
            )
            await db.flush()
    return sessions


async def get_active_sessions_for_user(
    db: AsyncSession, user_id: uuid.UUID
) -> list[Session]:
    with DatabaseTimer("select", "sessions"):
        result = await db.execute(
            select(Session).where(
                and_(
                    Session.user_id == user_id,
                    Session.is_revoked == False,  # noqa: E712
                    Session.expires_at > utcnow(),
                )
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# EmailToken CRUD
# ---------------------------------------------------------------------------


async def create_email_token(db: AsyncSession, token: EmailToken) -> EmailToken:
    with DatabaseTimer("insert", "email_tokens"):
        db.add(token)
        await db.flush()
    return token


async def get_valid_email_token(
    db: AsyncSession, token_hash: str, purpose: EmailTokenPurpose
) -> Optional[EmailToken]:
    with DatabaseTimer("select", "email_tokens"):
        result = await db.execute(
            select(EmailToken).where(
                and_(
                    EmailToken.token_hash == token_hash,
                    EmailToken.purpose == purpose,
                    EmailToken.used_at == None,  # noqa: E711
                    EmailToken.invalidated_at == None,  # noqa: E711
                    EmailToken.expires_at > utcnow(),
                )
            )
        )
        return result.scalars().first()


async def mark_email_token_used(db: AsyncSession, token_id: uuid.UUID) -> None:
    with DatabaseTimer("update", "email_tokens"):
        await db.execute(
            update(EmailToken).where(EmailToken.id == token_id).values(used_at=utcnow())
        )
        await db.flush()


async def invalidate_tokens_for_user_purpose(
    db: AsyncSession, user_id: uuid.UUID, purpose: EmailTokenPurpose
) -> None:
    """Soft-invalidate older tokens when a new one is issued."""
    with DatabaseTimer("update", "email_tokens"):
        await db.execute(
            update(EmailToken)
            .where(
                and_(
                    EmailToken.user_id == user_id,
                    EmailToken.purpose == purpose,
                    EmailToken.used_at == None,  # noqa: E711
                    EmailToken.invalidated_at == None,  # noqa: E711
                )
            )
            .values(invalidated_at=utcnow())
        )
        await db.flush()


# ---------------------------------------------------------------------------
# AuthAuditLog CRUD
# ---------------------------------------------------------------------------


async def create_audit_log(db: AsyncSession, log: AuthAuditLog) -> AuthAuditLog:
    with DatabaseTimer("insert", "auth_audit_log"):
        db.add(log)
        await db.flush()
    return log


# ---------------------------------------------------------------------------
# Redis – Refresh-token cache
# ---------------------------------------------------------------------------


async def cache_refresh_token(
    raw_token: str,
    data: RefreshTokenCache,
    redis_client: redis.Redis,
) -> None:
    """Cache a refresh token in Redis with TTL."""
    ttl = int((data.expires_at - datetime.now(timezone.utc)).total_seconds())
    if ttl <= 0:
        logger.warning("cache.refresh_token_expired_on_store")
        return
    await redis_client.set(_rt_key(raw_token), data.model_dump_json(), ex=ttl)
    logger.debug(
        "cache.refresh_token_cached", session_id=data.session_id, ttl_seconds=ttl
    )


async def get_cached_refresh_token(
    raw_token: str, redis_client: redis.Redis
) -> Optional[RefreshTokenCache]:
    """Return cached RefreshTokenCache or None if missing/expired."""
    data = await redis_client.get(_rt_key(raw_token))
    if data is None:
        return None
    return RefreshTokenCache.model_validate_json(data)


async def delete_cached_refresh_token(
    raw_token: str, redis_client: redis.Redis
) -> bool:
    """Delete a cached refresh token. Returns True if it existed."""
    deleted = await redis_client.delete(_rt_key(raw_token))
    return deleted > 0
