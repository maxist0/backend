"""add users.password_changed_at

The credential version of an account: set by every password change and
reset, never by a rehash. A login commits only if the value is still the
one it read together with the hash it verified, so a login racing a
password change gets no session. NULL means never changed, so existing rows
need no backfill.

A nullable column without a default is a catalog change in PostgreSQL: no
table rewrite, only a brief ACCESS EXCLUSIVE lock on `users`. The downgrade
drops the column and its values. Additive, so code that does not know the
column keeps working (expand step).

Revision ID: 57d93cf19427
Revises: b799370af501
Create Date: 2026-11-22 21:14:53.162197+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "57d93cf19427"
down_revision: str | Sequence[str] | None = "b799370af501"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable `users.password_changed_at`."""
    op.add_column(
        "users",
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop `users.password_changed_at`."""
    op.drop_column("users", "password_changed_at")
