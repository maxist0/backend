"""The nightly cleanup deletes ended sessions and closed email tokens only.

They go after the retention period, counted from the row's last change;
for a session, from its family's end at the earliest, so a replayed rotated
token is found as long as its family can still be refreshed. Nothing else
goes.

Rows are written with SQL, with timestamps relative to `now()`. The task
runs in-process (`run_task`) with the default retention of 30 days.
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth import crud, service
from app.modules.auth.tasks import cleanup_task
from tests.helpers import (
    Db,
    app_of,
    bearer,
    call_in_app,
    log_events,
    login_tokens,
    refresh_with,
    register_ok,
    run_task,
    user_id_of,
)

type _Delete = Callable[[AsyncSession, timedelta, int], Awaitable[int]]

# Relative to now(); the retention is 30 days.
OLD = "now() - interval '31 days'"
RECENT = "now() - interval '29 days'"
FUTURE = "now() + interval '5 days'"


def _session(
    db: Db,
    user_id: str,
    *,
    expires: str,
    revoked: str | None = None,
    family_ends: str | None = None,
    reason: str = "logout",
) -> str:
    """Insert a session; its family ends with it unless `family_ends` is later."""
    session_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions (id, user_id, token_hash, family_id, is_revoked, "
        "revoke_reason, expires_at, family_expires_at, revoked_at, created_at) "
        f"VALUES (:id, :u, :h, :f, :rev, :reason, {expires}, "
        f"{family_ends or expires}, {revoked or 'NULL'}, now())",
        id=session_id,
        u=user_id,
        h=uuid.uuid4().hex,
        f=str(uuid.uuid4()),
        rev=revoked is not None,
        reason=reason if revoked is not None else None,
    )
    return session_id


def _token(
    db: Db,
    user_id: str,
    *,
    expires: str,
    used: str | None = None,
    invalidated: str | None = None,
) -> str:
    token_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO email_tokens (id, user_id, purpose, token_hash, new_email, "
        "previous_email, expires_at, used_at, invalidated_at, created_at) VALUES "
        "(:id, :u, 'change_email', :h, 'new@example.com', 'old@example.com', "
        f"{expires}, {used or 'NULL'}, {invalidated or 'NULL'}, now())",
        id=token_id,
        u=user_id,
        h=uuid.uuid4().hex,
    )
    return token_id


def _ids(db: Db, table: str) -> set[str]:
    return {str(row["id"]) for row in db.rows(f"SELECT id FROM {table}")}


class TestCleanupTask:
    def test_deletes_exactly_the_rows_past_retention(
        self, client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        db.execute("DELETE FROM sessions")
        db.execute("DELETE FROM email_tokens")
        kept_sessions = {
            _session(db, user_id, expires=FUTURE),  # active
            _session(db, user_id, expires=RECENT),  # expired recently
            # Revoked long ago, but its token has not expired: a rotated
            # token that comes back must still be found (reuse detection).
            _session(db, user_id, expires=FUTURE, revoked=OLD),
            # Expired long ago, revoked recently (logout-all reaches it).
            _session(db, user_id, expires=OLD, revoked=RECENT),
            # Rotated and expired long ago, but its family ended recently:
            # a replay of the token must still revoke the family.
            _session(
                db,
                user_id,
                expires=OLD,
                revoked=OLD,
                family_ends=RECENT,
                reason="rotated",
            ),
            # Rotated long ago in a family that can still be refreshed.
            _session(
                db,
                user_id,
                expires=OLD,
                revoked=OLD,
                family_ends=FUTURE,
                reason="rotated",
            ),
        }
        gone_sessions = {
            _session(db, user_id, expires=OLD),
            _session(db, user_id, expires=OLD, revoked=OLD),
            # Rotated in a family that ended before the retention.
            _session(
                db,
                user_id,
                expires=OLD,
                revoked=OLD,
                family_ends=OLD,
                reason="rotated",
            ),
        }
        kept_tokens = {
            _token(db, user_id, expires=FUTURE),  # open
            _token(db, user_id, expires=FUTURE, used=OLD),  # used, not expired
            _token(db, user_id, expires=RECENT, used=RECENT),
            # Invalidated after it expired, recently.
            _token(db, user_id, expires=OLD, invalidated=RECENT),
        }
        gone_tokens = {
            _token(db, user_id, expires=OLD),
            _token(db, user_id, expires=OLD, used=OLD),
            _token(db, user_id, expires=OLD, invalidated=OLD),
        }
        db.execute("UPDATE auth_audit_log SET created_at = now() - interval '400 days'")
        audit_rows = _ids(db, "auth_audit_log")
        caplog.set_level(logging.INFO)

        run_task(client, cleanup_task)

        assert _ids(db, "sessions") == kept_sessions
        assert _ids(db, "email_tokens") == kept_tokens
        assert audit_rows
        assert _ids(db, "auth_audit_log") == audit_rows
        assert db.scalar("SELECT count(*) FROM users") == 1
        [event] = log_events(caplog, "auth.cleanup_done")
        assert event["sessions_deleted"] == len(gone_sessions)
        assert event["email_tokens_deleted"] == len(gone_tokens)
        assert event["retention_days"] == 30

    def test_second_run_finds_nothing(
        self, client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        _session(db, user_id, expires=OLD)
        _token(db, user_id, expires=OLD)
        caplog.set_level(logging.INFO)
        run_task(client, cleanup_task)
        caplog.clear()

        run_task(client, cleanup_task)

        [event] = log_events(caplog, "auth.cleanup_done")
        assert (event["sessions_deleted"], event["email_tokens_deleted"]) == (0, 0)

    def test_retention_setting_moves_the_cutoff(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        db.execute("DELETE FROM email_tokens")
        week_old = _token(db, user_id, expires="now() - interval '8 days'")
        monkeypatch.setenv("AUTH_CLEANUP_RETENTION_DAYS", "7")
        from app.core.config import get_settings

        get_settings.cache_clear()
        try:
            run_task(client, cleanup_task)
        finally:
            get_settings.cache_clear()

        assert week_old not in _ids(db, "email_tokens")

    def test_locked_rows_are_skipped_without_waiting(
        self, client: TestClient, db: Db, sync_engine: Engine
    ) -> None:
        """A row another transaction holds is left for the next run."""
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        held = _session(db, user_id, expires=OLD)
        free = _session(db, user_id, expires=OLD)

        with sync_engine.connect() as conn:
            conn.exec_driver_sql(
                "SELECT id FROM sessions WHERE id = %(id)s FOR UPDATE", {"id": held}
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(run_task, client, cleanup_task).result(timeout=30)
            conn.rollback()

        assert held in _ids(db, "sessions")
        assert free not in _ids(db, "sessions")
        run_task(client, cleanup_task)
        assert held not in _ids(db, "sessions")


class TestReuseDetectionOutlivesTheCleanup:
    @pytest.mark.usefixtures("captured_emails")
    def test_a_rotated_token_replayed_after_the_cleanup_revokes_its_family(
        self, client: TestClient, db: Db
    ) -> None:
        """The rotated session ended long ago, but its family has not."""
        register_ok(client, username="alice")
        old = login_tokens(client, "alice")
        r, successor = refresh_with(client, old.refresh)
        assert r.status_code == 200
        assert successor is not None
        db.execute(
            "UPDATE sessions SET expires_at = now() - interval '40 days', "
            "revoked_at = now() - interval '40 days' WHERE revoke_reason = 'rotated'"
        )
        assert db.scalar("SELECT bool_and(family_expires_at > now()) FROM sessions")

        run_task(client, cleanup_task)
        replay, _ = refresh_with(client, old.refresh)

        assert replay.status_code == 401
        assert "reuse" in replay.json()["detail"].lower()
        assert db.scalar("SELECT count(*) FROM sessions WHERE NOT is_revoked") == 0
        assert (
            db.scalar(
                "SELECT revoke_reason FROM sessions WHERE revoke_reason <> 'rotated'"
            )
            == "reuse_detected"
        )
        after, _ = refresh_with(client, successor.refresh)
        assert after.status_code == 401


class TestBatching:
    def test_each_batch_is_one_bounded_statement_and_commit(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        db.execute("DELETE FROM email_tokens")
        for _ in range(5):
            _session(db, user_id, expires=OLD)
        for _ in range(2):
            _token(db, user_id, expires=OLD)

        calls: list[tuple[str, int, int]] = []

        def _spy(name: str, real: _Delete) -> _Delete:
            async def _wrapped(
                session: AsyncSession, retention: timedelta, limit: int
            ) -> int:
                deleted = await real(session, retention, limit)
                calls.append((name, limit, deleted))
                return deleted

            return _wrapped

        monkeypatch.setattr(
            crud,
            "delete_ended_sessions",
            _spy("sessions", crud.delete_ended_sessions),
        )
        monkeypatch.setattr(
            crud,
            "delete_closed_email_tokens",
            _spy("email_tokens", crud.delete_closed_email_tokens),
        )
        commits: list[int] = []

        async def _cleanup() -> service.CleanupResult:
            async with app_of(client).state.session_factory() as session:
                real_commit = session.commit

                async def _counting_commit() -> None:
                    commits.append(len(calls))
                    await real_commit()

                session.commit = _counting_commit
                return await service.cleanup_ended_auth_records(
                    session, timedelta(days=30), batch_size=2
                )

        totals = call_in_app(client, _cleanup)

        assert totals == service.CleanupResult(5, 2, capped=False)
        assert calls == [
            ("sessions", 2, 2),
            ("sessions", 2, 2),
            ("sessions", 2, 1),
            ("email_tokens", 2, 2),
            ("email_tokens", 2, 0),
        ]
        # One commit right after every batch.
        assert commits == [1, 2, 3, 4, 5]
        assert db.scalar("SELECT count(*) FROM sessions") == 0


class TestRunCap:
    def test_a_run_stops_after_the_cap_and_the_next_one_continues(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        db.execute("DELETE FROM email_tokens")
        for _ in range(5):
            _session(db, user_id, expires=OLD)

        async def _cleanup() -> service.CleanupResult:
            async with app_of(client).state.session_factory() as session:
                return await service.cleanup_ended_auth_records(
                    session, timedelta(days=30), batch_size=2, max_batches=2
                )

        first = call_in_app(client, _cleanup)
        second = call_in_app(client, _cleanup)

        assert (first.sessions_deleted, first.capped) == (4, True)
        assert (second.sessions_deleted, second.capped) == (1, False)
        assert db.scalar("SELECT count(*) FROM sessions") == 0

    def test_cap_is_logged(
        self,
        client: TestClient,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        for _ in range(3):
            _session(db, user_id, expires=OLD)
        real = service.cleanup_ended_auth_records

        async def _small_run(
            session: AsyncSession, retention: timedelta
        ) -> service.CleanupResult:
            return await real(session, retention, batch_size=1, max_batches=2)

        monkeypatch.setattr(service, "cleanup_ended_auth_records", _small_run)
        caplog.set_level(logging.INFO)

        run_task(client, cleanup_task)

        [event] = log_events(caplog, "auth.cleanup_done")
        assert (event["sessions_deleted"], event["capped"]) == (2, True)


class TestRevocationLeavesExpiredSessions:
    def test_logout_all_does_not_touch_expired_sessions(
        self, client: TestClient, db: Db
    ) -> None:
        """Their retention keeps counting from the expiry."""
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        expired = _session(db, user_id, expires=RECENT)
        tokens = login_tokens(client, "alice")

        r = client.post("/api/v1/auth/logout-all", headers=bearer(tokens.access))

        assert r.status_code == 204
        row = db.one(
            "SELECT is_revoked, revoked_at FROM sessions WHERE id = :s", s=expired
        )
        assert (row["is_revoked"], row["revoked_at"]) == (False, None)
        assert (
            db.scalar(
                "SELECT detail FROM auth_audit_log WHERE detail LIKE 'Logout all%'"
            )
            == "Logout all, revoked 1 session(s)"
        )
