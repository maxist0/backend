"""add_admin_audit_event_types

Adds the audit event types of the admin actions to the native enum
`auditeventtype`: ACCOUNT_DEACTIVATED, ACCOUNT_REACTIVATED and
PASSWORD_CHANGE_FORCED. The labels are the Python member names, like the
existing ones.

Upgrade: `ALTER TYPE ... ADD VALUE IF NOT EXISTS` changes only the catalog
(no table lock, no rewrite) and is instant. PostgreSQL accepts it inside the
migration transaction, but the new values cannot be used before that
transaction commits, so this revision does not use them, and a later
revision in the same `alembic upgrade` run must not either.

Downgrade: PostgreSQL cannot drop enum values, so the type is rebuilt
without them. Rows that use them are kept and relabelled to the nearest old
type, with the original type prepended to `detail` (audit evidence is never
deleted). The column type change rewrites `auth_audit_log` and its indexes
under an ACCESS EXCLUSIVE lock; the duration grows with the table size.

Revision ID: 8b82d1b725c0
Revises: 37a0d49f8bbc
Create Date: 2026-11-17 11:08:38.590180+00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "8b82d1b725c0"
down_revision: str | None = "37a0d49f8bbc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

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
)

# New label -> the old label its rows get on downgrade.
_DOWNGRADE_MAP = {
    "ACCOUNT_DEACTIVATED": "ACCOUNT_LOCKED",
    "ACCOUNT_REACTIVATED": "ACCOUNT_UNLOCKED",
    "PASSWORD_CHANGE_FORCED": "PASSWORD_RESET_REQUESTED",
}


def upgrade() -> None:
    for label in _DOWNGRADE_MAP:
        op.execute(f"ALTER TYPE auditeventtype ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    # Fail fast instead of queueing behind long transactions: a waiting
    # ACCESS EXCLUSIVE request blocks every later audit insert (logins).
    op.execute("SET LOCAL lock_timeout = '5s'")
    # Taken first, so no row with a new label can be inserted between the
    # relabelling and the type change.
    op.execute("LOCK TABLE auth_audit_log IN ACCESS EXCLUSIVE MODE")
    for new, old in _DOWNGRADE_MAP.items():
        op.execute(
            f"UPDATE auth_audit_log SET event_type = '{old}', "
            f"detail = concat_ws(' ', '[{new.lower()}]', detail) "
            f"WHERE event_type::text = '{new}'"
        )
    labels = ", ".join(f"'{label}'" for label in _OLD_LABELS)
    op.execute("ALTER TYPE auditeventtype RENAME TO auditeventtype_old")
    op.execute(f"CREATE TYPE auditeventtype AS ENUM ({labels})")
    op.execute(
        "ALTER TABLE auth_audit_log ALTER COLUMN event_type TYPE auditeventtype "
        "USING event_type::text::auditeventtype"
    )
    op.execute("DROP TYPE auditeventtype_old")
