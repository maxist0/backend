"""The audit event types in PostgreSQL match the model, and the migration
that added the admin event types survives upgrade, downgrade and upgrade.

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


def test_round_trip_keeps_every_audit_row(scratch_db):
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    user_id = uuid.uuid4()
    rows = [
        ("ACCOUNT_DEACTIVATED", "Deactivated by admin root"),
        ("ACCOUNT_REACTIVATED", None),
        ("PASSWORD_CHANGE_FORCED", "Password change forced by admin root"),
        ("LOGIN_SUCCESS", None),
    ]
    with scratch_db.begin() as conn:
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

    command.downgrade(cfg, "-1")

    assert _labels(scratch_db) == [
        label for label in MODEL_LABELS if label not in ADMIN_EVENT_LABELS
    ]
    with scratch_db.connect() as conn:
        kept = conn.execute(
            text(
                "SELECT event_type::text, detail FROM auth_audit_log ORDER BY created_at"
            )
        ).all()
    assert [tuple(row) for row in kept] == [
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
