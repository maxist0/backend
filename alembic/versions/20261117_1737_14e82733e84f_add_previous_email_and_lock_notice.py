"""add_previous_email_and_lock_notice

Adds what the security notices and the cleanup task need:

* `email_tokens.previous_email` (nullable): the address an email change
  replaced, written when the change is confirmed and the old address was
  verified, for the notice to it. The cleanup task deletes it with its
  token.
* the audit event type ACCOUNT_LOCKED_NOTICE_SENT in the native enum
  `auditeventtype` (label = Python member name, like the others): written
  when the account-locked mail went out; it throttles that mail.
* the index `ix_sessions_expires_at`, which the cleanup task's batched
  deletes scan oldest first.

Upgrade: adding a nullable column without a default and adding an enum
value change only the catalog, but still need short exclusive locks; a
5 s `lock_timeout` makes the migration fail instead of queueing every
login behind a long transaction. `ALTER TYPE ... ADD VALUE` works inside
the migration transaction, but the value cannot be used before that
transaction commits, so no later revision of the same run may use it.

`CREATE INDEX` blocks writes to `sessions` (logins, refreshes) while it
builds. On a large table, build it by hand first:

    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_sessions_expires_at
        ON sessions (expires_at);
    SELECT indisvalid FROM pg_index
        WHERE indexrelid = 'ix_sessions_expires_at'::regclass;

`IF NOT EXISTS` makes this revision skip an existing index, an INVALID one
left by a failed concurrent build included. If `indisvalid` is false,
`DROP INDEX CONCURRENTLY ix_sessions_expires_at` and build it again. Do not
migrate around 03:00 UTC, when the cleanup task deletes from `sessions`.

Downgrade: the `auth_audit_log` lock is taken first, so a lock wait fails
within 5 s before anything else is locked; then the column and the index
are dropped. PostgreSQL cannot drop an enum value, so the type is rebuilt
without it; notice rows are kept and relabelled ACCOUNT_LOCKED, with
`[account_locked_notice_sent]` prepended to `detail` (audit rows are never
deleted). The column type change rewrites `auth_audit_log` and its indexes
under an ACCESS EXCLUSIVE lock.

Revision ID: 14e82733e84f
Revises: 8b82d1b725c0
Create Date: 2026-11-17 17:37:22.201138+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "14e82733e84f"
down_revision: str | None = "8b82d1b725c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_LABEL = "ACCOUNT_LOCKED_NOTICE_SENT"
# The label its rows get on downgrade.
_DOWNGRADE_LABEL = "ACCOUNT_LOCKED"
_OLD_LABELS = (
    "REGISTER",
    "LOGIN_SUCCESS",
    "LOGIN_FAILED",
    "ACCOUNT_LOCKED",
    "ACCOUNT_UNLOCKED",
    "EMAIL_VERIFICATION_SENT",
    "EMAIL_VERIFIED",
    "EMAIL_CHANGED",
    "PASSWORD_CHANGED",
    "PASSWORD_RESET_REQUESTED",
    "PASSWORD_RESET_COMPLETED",
    "SESSION_REVOKED",
    "ACCOUNT_DEACTIVATED",
    "ACCOUNT_REACTIVATED",
    "PASSWORD_CHANGE_FORCED",
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "email_tokens",
        sa.Column("previous_email", sa.String(length=255), nullable=True),
    )
    op.execute(f"ALTER TYPE auditeventtype ADD VALUE IF NOT EXISTS '{_NEW_LABEL}'")
    op.create_index(
        "ix_sessions_expires_at",
        "sessions",
        ["expires_at"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    # Fail fast instead of queueing behind long transactions: a waiting
    # ACCESS EXCLUSIVE request blocks every later audit insert (logins).
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Taken first: no row with the new label can be inserted between the
    # relabelling and the type change, and a wait for it fails before the
    # `sessions` and `email_tokens` locks below are held (logins would
    # queue behind them).
    op.execute("LOCK TABLE auth_audit_log IN ACCESS EXCLUSIVE MODE")
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_column("email_tokens", "previous_email")
    op.execute(
        f"UPDATE auth_audit_log SET event_type = '{_DOWNGRADE_LABEL}', "
        f"detail = concat_ws(' ', '[{_NEW_LABEL.lower()}]', detail) "
        f"WHERE event_type::text = '{_NEW_LABEL}'"
    )
    labels = ", ".join(f"'{label}'" for label in _OLD_LABELS)
    op.execute("ALTER TYPE auditeventtype RENAME TO auditeventtype_old")
    op.execute(f"CREATE TYPE auditeventtype AS ENUM ({labels})")
    op.execute(
        "ALTER TABLE auth_audit_log ALTER COLUMN event_type TYPE auditeventtype "
        "USING event_type::text::auditeventtype"
    )
    op.execute("DROP TYPE auditeventtype_old")
