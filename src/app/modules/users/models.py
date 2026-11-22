"""ORM model of the users module: the `User` entity.

Other modules reference users by foreign key and may keep a many-to-one
relationship to `User`; `User` itself has no relationships to them, so this
module depends on no other module. Deleting a user removes the dependent
rows through the foreign keys' `ON DELETE` rules.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core import Base, Timestamps, UUIDPrimaryKey, utcnow

# Names of the unique constraints, for telling the conflicts apart
# (`constraint_name`). The email constraint is named by the core's naming
# convention; the username index is named here.
USERNAME_UNIQUE_INDEX = "uq_users_lower_username"
EMAIL_UNIQUE_CONSTRAINT = "uq_users_email"


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


class User(UUIDPrimaryKey, Timestamps, Base):
    """A user account; credentials and sessions belong to the auth module."""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), nullable=False)
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
    # Credential version: set by every password change or reset, never by a
    # rehash. A login only succeeds if it is still the value read together
    # with the hash it verified. NULL: never changed.
    password_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        # Usernames are unique regardless of case and keep the casing the
        # user chose. The login lookup filters on the same expression.
        Index(USERNAME_UNIQUE_INDEX, func.lower(username), unique=True),
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

    def password_changed_after(self, issued_at: int) -> bool:
        """Return True if the password changed after a token's `iat`.

        `issued_at` is a JWT `iat` in whole seconds, so the change time is
        cut to whole seconds too: a token issued in the same second as the
        change counts as newer, even if it predates the change by up to a
        second.
        """
        if self.password_changed_at is None:
            return False
        return issued_at < int(self.password_changed_at.timestamp())

    def is_admin_locked(self) -> bool:
        """Return True for the indefinite (admin) lock: `locked_until IS NULL`.

        Only this lock ends existing sessions: access tokens and refresh are
        rejected. A timed lock from failed logins blocks the password login
        only (`is_lock_active`), so guessing cannot log the real user out.
        """
        return self.is_locked and self.locked_until is None
