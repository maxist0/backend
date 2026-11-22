"""Data-access layer of the users module: queries on `users`."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import utcnow
from app.modules.users.models import User


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Return the user with this id, or None."""
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalars().first()


async def get_user_by_username(db: AsyncSession, username: str) -> User | None:
    """Return the user with this username in any casing, or None.

    `lower(username)` is the expression of the unique index, so the lookup
    uses it and finds at most one user.
    """
    result = await db.execute(
        select(User).where(func.lower(User.username) == func.lower(username))
    )
    return result.scalars().first()


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    """Return the user with this email address (stored lower case), or None."""
    result = await db.execute(select(User).where(User.email == email))
    return result.scalars().first()


async def create_user(db: AsyncSession, user: User) -> User:
    """Insert `user` and commit."""
    db.add(user)
    await db.commit()
    return user


async def update_user(db: AsyncSession, user: User) -> User:
    """Stamp `updated_at` on `user` and commit."""
    user.updated_at = utcnow()
    db.add(user)
    await db.commit()
    return user
