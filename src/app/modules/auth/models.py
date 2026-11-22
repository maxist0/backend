"""ORM models of the auth module: credentials, sessions, email tokens, audit log.

Each row belongs to a `users.User`. The relationships point from here to
`User` only (no `back_populates`), so the users module does not depend on
auth. The foreign keys' `ON DELETE` rules remove or detach these rows when a
user is deleted.
"""

import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import Base, CreatedAt, Timestamps, UUIDPrimaryKey, value_enum
from app.modules.users import User


class PasswordCredential(UUIDPrimaryKey, Timestamps, Base):
    """The Argon2 password hash of a user (one per user)."""

    __tablename__ = "password_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    must_change: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    def __str__(self) -> str:
        """Label in the admin panel, without the hash."""
        return f"PasswordCredential(user_id={self.user_id})"

    user: Mapped[User] = relationship()


class AuditEventType(PyEnum):
    """Kind of an audit-log entry; the column stores the values."""

    REGISTER = "register"
    LOGIN_SUCCESS = "login_success"
    LOGIN_FAILED = "login_failed"
    ACCOUNT_LOCKED = "account_locked"
    ACCOUNT_UNLOCKED = "account_unlocked"
    EMAIL_VERIFICATION_SENT = "email_verification_sent"
    EMAIL_VERIFIED = "email_verified"
    EMAIL_CHANGED = "email_changed"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 - audit event name, not a secret
    PASSWORD_RESET_REQUESTED = "password_reset_requested"  # noqa: S105 - audit event name, not a secret
    PASSWORD_RESET_COMPLETED = "password_reset_completed"  # noqa: S105 - audit event name, not a secret
    SESSION_REVOKED = "session_revoked"
    ACCOUNT_DEACTIVATED = "account_deactivated"
    ACCOUNT_REACTIVATED = "account_reactivated"
    PASSWORD_CHANGE_FORCED = "password_change_forced"  # noqa: S105 - audit event name, not a secret
    # Written when the account-locked mail went out; it throttles that mail.
    ACCOUNT_LOCKED_NOTICE_SENT = "account_locked_notice_sent"


class AuthAuditLog(UUIDPrimaryKey, CreatedAt, Base):
    """An append-only record of a security-relevant event of an account."""

    __tablename__ = "auth_audit_log"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[AuditEventType] = mapped_column(
        value_enum(AuditEventType), nullable=False
    )
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __str__(self) -> str:
        """Label in the admin panel."""
        return f"{self.event_type.value} @ {self.created_at}"

    user: Mapped[User | None] = relationship()

    __table_args__ = (
        Index("ix_audit_log_user_id", "user_id"),
        Index("ix_audit_log_created_at", "created_at"),
        Index("ix_audit_log_event_type", "event_type"),
    )


class EmailTokenPurpose(PyEnum):
    """What an email token proves; the column stores the values."""

    VERIFY_EMAIL = "verify_email"
    CHANGE_EMAIL = "change_email"
    RESET_PASSWORD = "reset_password"  # noqa: S105 - token purpose, not a secret


class EmailToken(UUIDPrimaryKey, CreatedAt, Base):
    """A single-use token mailed to the user; only its SHA-256 hash is stored."""

    __tablename__ = "email_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    purpose: Mapped[EmailTokenPurpose] = mapped_column(
        value_enum(EmailTokenPurpose), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    new_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The address an email change replaced, set when the change is
    # confirmed, so the worker can notify the old address afterwards.
    previous_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __str__(self) -> str:
        """Label in the admin panel, without the hash."""
        return f"{self.purpose.value} (user_id={self.user_id})"

    user: Mapped[User] = relationship()

    __table_args__ = (
        Index("ix_email_tokens_token_hash", "token_hash"),
        Index("ix_email_tokens_expires_at", "expires_at"),
        Index("ix_email_tokens_user_purpose", "user_id", "purpose"),
    )


class Session(UUIDPrimaryKey, CreatedAt, Base):
    """A refresh-token session; rotated tokens stay for reuse detection."""

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    is_revoked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    revoke_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    # Absolute end of the login's rotation family: set at login, copied
    # unchanged on every rotation. No session of the family expires later
    # (CHECK below), so after this time no token of the family is accepted.
    family_expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    device_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __str__(self) -> str:
        """Label in the admin panel, without the hash."""
        status = "revoked" if self.is_revoked else "active"
        device = self.device_name or "unknown"
        return f"Session({status}, device={device}, ip={self.ip_address})"

    user: Mapped[User] = relationship()

    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_family_id", "family_id"),
        # The cleanup task deletes in batches ordered by this column.
        Index("ix_sessions_family_expires_at", "family_expires_at"),
        CheckConstraint(
            "expires_at <= family_expires_at", name="expires_within_family"
        ),
    )
