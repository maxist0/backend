"""The audit event types in PostgreSQL match the model, and the migrations
that added event types survive upgrade, downgrade and upgrade.

`alembic check` cannot see enum values, so the label comparison is the drift
guard for `auditeventtype`. The round trip runs in a throwaway database of
the session's container, so the shared test schema is never downgraded.
Alembic gets a `Config` without an ini file: the ini's logging setup would
disable the loggers other tests assert on.
"""

import os
import uuid
from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, text

from app.modules.auth.models import AuditEventType
from tests.helpers import BACKEND_DIR

ADMIN_EVENT_LABELS = (
    "ACCOUNT_DEACTIVATED",
    "ACCOUNT_REACTIVATED",
    "PASSWORD_CHANGE_FORCED",
)
NOTICE_EVENT_LABEL = "ACCOUNT_LOCKED_NOTICE_SENT"
# The revisions before the one that added the admin types, and before the
# one that added the lock notice type, `email_tokens.previous_email` and
# `ix_sessions_expires_at`.
BEFORE_ADMIN_TYPES = "37a0d49f8bbc"
BEFORE_LOCK_NOTICE = "8b82d1b725c0"
MODEL_LABELS = [member.name for member in AuditEventType]


def _labels(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT enumlabel FROM pg_enum "
                    "WHERE enumtypid = 'auditeventtype'::regtype ORDER BY enumsortorder"
                )
            ).scalars()
        )


def _alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    return cfg


@pytest.fixture
def scratch_db(sync_engine, monkeypatch) -> Iterator[Engine]:
    """An empty database; `DATABASE_URL` points at it for Alembic."""
    name = f"migration_{uuid.uuid4().hex[:12]}"
    admin = sync_engine.execution_options(isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(os.environ["DATABASE_URL"]).set(database=name)
    rendered = url.render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", rendered)
    engine = create_engine(rendered)
    try:
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))


def test_database_labels_match_the_model(sync_engine):
    assert _labels(sync_engine) == MODEL_LABELS


def _insert_user_and_audit_rows(
    engine: Engine, rows: list[tuple[str, str | None]]
) -> None:
    user_id = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, username, email, is_active, "
                "is_email_verified, is_locked, failed_login_attempts, created_at, "
                "updated_at) VALUES (:id, 'u', 'u@example.com', true, false, false, "
                "0, now(), now())"
            ),
            {"id": user_id},
        )
        for position, (label, detail) in enumerate(rows):
            conn.execute(
                text(
                    "INSERT INTO auth_audit_log (id, user_id, event_type, detail, "
                    "created_at) VALUES (:id, :user_id, CAST(:label AS auditeventtype), "
                    ":detail, now() + make_interval(secs => :position))"
                ),
                {
                    "id": uuid.uuid4(),
                    "user_id": user_id,
                    "label": label,
                    "detail": detail,
                    "position": position,
                },
            )


def _audit_rows(engine: Engine) -> list[tuple[str, str | None]]:
    with engine.connect() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                text(
                    "SELECT event_type::text, detail FROM auth_audit_log "
                    "ORDER BY created_at"
                )
            )
        ]


def _columns(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = :t"
                ),
                {"t": table},
            ).scalars()
        )


def _indexes(engine: Engine, table: str) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = :t"),
                {"t": table},
            ).scalars()
        )


def test_lock_notice_round_trip_keeps_every_audit_row(scratch_db):
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    _insert_user_and_audit_rows(
        scratch_db,
        [
            (NOTICE_EVENT_LABEL, "Lock notice sent"),
            ("ACCOUNT_LOCKED", "Locked after 5 failed attempts"),
        ],
    )

    command.downgrade(cfg, BEFORE_LOCK_NOTICE)

    assert _labels(scratch_db) == [
        label for label in MODEL_LABELS if label != NOTICE_EVENT_LABEL
    ]
    assert _audit_rows(scratch_db) == [
        ("ACCOUNT_LOCKED", "[account_locked_notice_sent] Lock notice sent"),
        ("ACCOUNT_LOCKED", "Locked after 5 failed attempts"),
    ]
    assert "previous_email" not in _columns(scratch_db, "email_tokens")
    assert "ix_sessions_expires_at" not in _indexes(scratch_db, "sessions")

    command.upgrade(cfg, "head")

    assert _labels(scratch_db) == MODEL_LABELS
    assert "previous_email" in _columns(scratch_db, "email_tokens")
    assert "ix_sessions_expires_at" in _indexes(scratch_db, "sessions")
    command.check(cfg)


def test_admin_types_round_trip_keeps_every_audit_row(scratch_db):
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    _insert_user_and_audit_rows(
        scratch_db,
        [
            ("ACCOUNT_DEACTIVATED", "Deactivated by admin root"),
            ("ACCOUNT_REACTIVATED", None),
            ("PASSWORD_CHANGE_FORCED", "Password change forced by admin root"),
            ("LOGIN_SUCCESS", None),
        ],
    )

    command.downgrade(cfg, BEFORE_ADMIN_TYPES)

    assert _labels(scratch_db) == [
        label
        for label in MODEL_LABELS
        if label not in (*ADMIN_EVENT_LABELS, NOTICE_EVENT_LABEL)
    ]
    assert _audit_rows(scratch_db) == [
        ("ACCOUNT_LOCKED", "[account_deactivated] Deactivated by admin root"),
        ("ACCOUNT_UNLOCKED", "[account_reactivated]"),
        (
            "PASSWORD_RESET_REQUESTED",
            "[password_change_forced] Password change forced by admin root",
        ),
        ("LOGIN_SUCCESS", None),
    ]

    command.upgrade(cfg, "head")

    assert _labels(scratch_db) == MODEL_LABELS
    command.check(cfg)
