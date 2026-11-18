"""The nightly cleanup: ended sessions and closed email tokens go, after the
retention period counted from the row's last change; nothing else goes.

Rows are written with SQL, with timestamps relative to `now()`. The task
runs in-process (`run_task`) with the default retention of 30 days.
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from app.modules.auth import crud, service
from app.modules.auth.tasks import cleanup_task
from tests.helpers import bearer, login_tokens, register_ok, user_id_of
from tests.integration.conftest import run_task

# Relative to now(); the retention is 30 days.
OLD = "now() - interval '31 days'"
RECENT = "now() - interval '29 days'"
FUTURE = "now() + interval '5 days'"


def _session(db, user_id: str, *, expires: str, revoked: str | None = None) -> str:
    session_id = str(uuid.uuid4())
    db.execute(
        "INSERT INTO sessions (id, user_id, token_hash, family_id, is_revoked, "
        f"revoke_reason, expires_at, revoked_at, created_at) VALUES (:id, :u, :h, "
        f":f, :rev, :reason, {expires}, {revoked or 'NULL'}, now())",
        id=session_id,
        u=user_id,
        h=uuid.uuid4().hex,
        f=str(uuid.uuid4()),
        rev=revoked is not None,
        reason="logout" if revoked is not None else None,
    )
    return session_id


def _token(
    db,
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
        "(:id, :u, 'CHANGE_EMAIL', :h, 'new@example.com', 'old@example.com', "
        f"{expires}, {used or 'NULL'}, {invalidated or 'NULL'}, now())",
        id=token_id,
        u=user_id,
        h=uuid.uuid4().hex,
    )
    return token_id


def _ids(db, table: str) -> set[str]:
    return {str(row["id"]) for row in db.rows(f"SELECT id FROM {table}")}  # noqa: S608


def _events(caplog, name: str) -> list[dict]:
    return [
        r.msg
        for r in caplog.records
        if isinstance(r.msg, dict) and r.msg.get("event") == name
    ]


class TestCleanupTask:
    def test_deletes_exactly_the_rows_past_retention(self, client, db, caplog):
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
        }
        gone_sessions = {
            _session(db, user_id, expires=OLD),
            _session(db, user_id, expires=OLD, revoked=OLD),
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
        assert audit_rows and _ids(db, "auth_audit_log") == audit_rows
        assert db.scalar("SELECT count(*) FROM users") == 1
        [event] = _events(caplog, "auth.cleanup_done")
        assert event["sessions_deleted"] == len(gone_sessions)
        assert event["email_tokens_deleted"] == len(gone_tokens)
        assert event["retention_days"] == 30

    def test_second_run_finds_nothing(self, client, db, caplog):
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        _session(db, user_id, expires=OLD)
        _token(db, user_id, expires=OLD)
        caplog.set_level(logging.INFO)
        run_task(client, cleanup_task)
        caplog.clear()

        run_task(client, cleanup_task)

        [event] = _events(caplog, "auth.cleanup_done")
        assert (event["sessions_deleted"], event["email_tokens_deleted"]) == (0, 0)

    def test_retention_setting_moves_the_cutoff(self, client, db, monkeypatch):
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

    def test_locked_rows_are_skipped_without_waiting(self, client, db, sync_engine):
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


class TestBatching:
    def test_each_batch_is_one_bounded_statement_and_commit(
        self, client, db, monkeypatch
    ):
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        db.execute("DELETE FROM email_tokens")
        for _ in range(5):
            _session(db, user_id, expires=OLD)
        for _ in range(2):
            _token(db, user_id, expires=OLD)

        calls: list[tuple[str, int, int]] = []

        def _spy(name, real):
            async def _wrapped(session, retention, limit):
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
            async with client.app.state.session_factory() as session:
                real_commit = session.commit

                async def _counting_commit() -> None:
                    commits.append(len(calls))
                    await real_commit()

                session.commit = _counting_commit
                return await service.cleanup_ended_auth_records(
                    session, timedelta(days=30), batch_size=2
                )

        totals = client.portal.call(_cleanup)

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
    def test_a_run_stops_after_the_cap_and_the_next_one_continues(self, client, db):
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        db.execute("DELETE FROM email_tokens")
        for _ in range(5):
            _session(db, user_id, expires=OLD)

        async def _cleanup() -> service.CleanupResult:
            async with client.app.state.session_factory() as session:
                return await service.cleanup_ended_auth_records(
                    session, timedelta(days=30), batch_size=2, max_batches=2
                )

        first = client.portal.call(_cleanup)
        second = client.portal.call(_cleanup)

        assert (first.sessions_deleted, first.capped) == (4, True)
        assert (second.sessions_deleted, second.capped) == (1, False)
        assert db.scalar("SELECT count(*) FROM sessions") == 0

    def test_cap_is_logged(self, client, db, caplog, monkeypatch):
        register_ok(client, username="alice")
        user_id = user_id_of(db, "alice")
        for _ in range(3):
            _session(db, user_id, expires=OLD)
        real = service.cleanup_ended_auth_records

        async def _small_run(session, retention):
            return await real(session, retention, batch_size=1, max_batches=2)

        monkeypatch.setattr(service, "cleanup_ended_auth_records", _small_run)
        caplog.set_level(logging.INFO)

        run_task(client, cleanup_task)

        [event] = _events(caplog, "auth.cleanup_done")
        assert (event["sessions_deleted"], event["capped"]) == (2, True)


class TestRevocationLeavesExpiredSessions:
    def test_logout_all_does_not_touch_expired_sessions(self, client, db):
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
