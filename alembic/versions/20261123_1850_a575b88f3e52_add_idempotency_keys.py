"""add idempotency_keys

The core table behind `Idempotency-Key` (ADR 0014): one row per used key and
scope, inserted in the request's own transaction, with the request's HMAC
fingerprint and, once known, the response to replay. The unique constraint
on `(scope, key)` is what a parallel request with the same key waits on.
`created_at` is indexed for the hourly cleanup.

A new, empty table: no lock on existing tables. The downgrade drops the
table with its rows; clients then lose their replays for at most
`IDEMPOTENCY_RETENTION_HOURS`.

Revision ID: a575b88f3e52
Revises: 57d93cf19427
Create Date: 2026-11-23 18:50:02.825772+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a575b88f3e52"
down_revision: str | Sequence[str] | None = "57d93cf19427"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create `idempotency_keys` with its constraints and index."""
    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(length=300), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("fingerprint", sa.LargeBinary(), nullable=False),
        sa.Column("response_status", sa.SmallInteger(), nullable=True),
        sa.Column(
            "response_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("response_body", sa.LargeBinary(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(response_status IS NULL) = (response_headers IS NULL) "
            "AND (response_status IS NULL) = (response_body IS NULL)",
            name=op.f("ck_idempotency_keys_response_complete"),
        ),
        sa.CheckConstraint(
            "response_status BETWEEN 100 AND 599",
            name=op.f("ck_idempotency_keys_response_status_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_keys")),
        sa.UniqueConstraint("scope", "key", name=op.f("uq_idempotency_keys_scope")),
    )
    op.create_index(
        "ix_idempotency_keys_created_at",
        "idempotency_keys",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop `idempotency_keys`."""
    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
