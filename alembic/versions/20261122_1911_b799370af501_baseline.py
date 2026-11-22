"""baseline

Creates the complete schema of the `users` and `auth` modules in one
revision. It replaces the earlier revisions `37a0d49f8bbc`, `8b82d1b725c0`
and `14e82733e84f`; a database still stamped with one of them must be
recreated (`docker compose -f compose.dev.yaml down -v`), because no data
existed on any of them.

Names follow the naming convention on `Base.metadata` and are spelled out
here, so replaying this file never depends on the convention. Enum columns
are VARCHAR with a CHECK constraint on the values (`value_enum`); there are
no PostgreSQL enum types, so the downgrade leaves nothing behind.

Upgrade and downgrade create or drop empty tables only: no data, no rewrite,
locks only on the new tables. Not part of an expand/contract sequence.

Revision ID: b799370af501
Revises:
Create Date: 2026-11-22 19:11:49.526159+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b799370af501"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_email_verified", sa.Boolean(), nullable=False),
        sa.Column("is_locked", sa.Boolean(), nullable=False),
        sa.Column("failed_login_attempts", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index(
        "uq_users_lower_username",
        "users",
        [sa.literal_column("lower(username)")],
        unique=True,
    )
    op.create_table(
        "auth_audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'register', "
            "'login_success', "
            "'login_failed', "
            "'account_locked', "
            "'account_unlocked', "
            "'email_verification_sent', "
            "'email_verified', "
            "'email_changed', "
            "'password_changed', "
            "'password_reset_requested', "
            "'password_reset_completed', "
            "'session_revoked', "
            "'account_deactivated', "
            "'account_reactivated', "
            "'password_change_forced', "
            "'account_locked_notice_sent'"
            ")",
            name=op.f("ck_auth_audit_log_auditeventtype"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_auth_audit_log_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_audit_log")),
    )
    op.create_index(
        "ix_audit_log_created_at", "auth_audit_log", ["created_at"], unique=False
    )
    op.create_index(
        "ix_audit_log_event_type", "auth_audit_log", ["event_type"], unique=False
    )
    op.create_index("ix_audit_log_user_id", "auth_audit_log", ["user_id"], unique=False)
    op.create_table(
        "email_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("new_email", sa.String(length=255), nullable=True),
        sa.Column("previous_email", sa.String(length=255), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('verify_email', 'change_email', 'reset_password')",
            name=op.f("ck_email_tokens_emailtokenpurpose"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_tokens")),
    )
    op.create_index(
        "ix_email_tokens_expires_at", "email_tokens", ["expires_at"], unique=False
    )
    op.create_index(
        "ix_email_tokens_token_hash", "email_tokens", ["token_hash"], unique=False
    )
    op.create_index(
        "ix_email_tokens_user_purpose",
        "email_tokens",
        ["user_id", "purpose"],
        unique=False,
    )
    op.create_table(
        "password_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("must_change", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_password_credentials_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_password_credentials")),
        sa.UniqueConstraint("user_id", name=op.f("uq_password_credentials_user_id")),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("is_revoked", sa.Boolean(), nullable=False),
        sa.Column("revoke_reason", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("family_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("device_id", sa.String(length=255), nullable=True),
        sa.Column("device_name", sa.String(length=255), nullable=True),
        sa.Column("ip_address", sa.String(length=45), nullable=True),
        sa.Column("user_agent", sa.String(length=512), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "expires_at <= family_expires_at",
            name=op.f("ck_sessions_expires_within_family"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(
        "ix_sessions_family_expires_at", "sessions", ["family_expires_at"], unique=False
    )
    op.create_index("ix_sessions_family_id", "sessions", ["family_id"], unique=False)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_index("ix_sessions_family_id", table_name="sessions")
    op.drop_index("ix_sessions_family_expires_at", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("password_credentials")
    op.drop_index("ix_email_tokens_user_purpose", table_name="email_tokens")
    op.drop_index("ix_email_tokens_token_hash", table_name="email_tokens")
    op.drop_index("ix_email_tokens_expires_at", table_name="email_tokens")
    op.drop_table("email_tokens")
    op.drop_index("ix_audit_log_user_id", table_name="auth_audit_log")
    op.drop_index("ix_audit_log_event_type", table_name="auth_audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="auth_audit_log")
    op.drop_table("auth_audit_log")
    op.drop_index("uq_users_lower_username", table_name="users")
    op.drop_table("users")
