"""ORM model of the users module: the `User` entity.

Other modules reference users by foreign key and may keep a many-to-one
relationship to `User`; `User` itself has no relationships to them, so this
module depends on no other module. Deleting a user removes the dependent
rows through the foreign keys' `ON DELETE` rules.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core import Base, utcnow


def lock_is_active(
    is_locked: bool,  # noqa: FBT001 - callers unpack the `(is_locked, locked_until)` row
    locked_until: datetime | None,
    now: datetime | None = None,
) -> bool:
    """Lock rule shared by `User.is_lock_active` and fresh column reads.

    `is_locked` with `locked_until IS NULL` is an indefinite (admin) lock;
    a timed lock ends once `locked_until` lies in the past.
    """
    if not is_locked:
        return False
    if locked_until is None:
        return True
    return locked_until > (now or utcnow())


class User(Base):
    """A user account; credentials and sessions belong to the auth module."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_email_verified: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_login_attempts: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __str__(self) -> str:
        """Label of the user in the admin panel."""
        return f"{self.username} ({self.email})"

    def is_lock_active(self, now: datetime | None = None) -> bool:
        """Return True while the account is locked.

        `is_locked` with `locked_until IS NULL` is an indefinite (admin)
        lock. A timed lock ends once `locked_until` lies in the past.
        """
        return lock_is_active(self.is_locked, self.locked_until, now)

    def is_admin_locked(self) -> bool:
        """Return True for the indefinite (admin) lock: `locked_until IS NULL`.

        Only this lock ends existing sessions: access tokens and refresh are
        rejected. A timed lock from failed logins blocks the password login
        only (`is_lock_active`), so guessing cannot log the real user out.
        """
        return self.is_locked and self.locked_until is None
