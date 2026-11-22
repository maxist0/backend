"""The refresh gate: PostgreSQL decides atomically which refresh wins.

A rotated token used again revokes the whole family; revoked, expired,
deactivated and admin-locked sessions refresh no more. Redis holds no
session state.
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.helpers import (
    Db,
    active_session_count,
    assert_nothing_personal_in_redis,
    login_tokens,
    refresh_with,
    register_ok,
    run_parallel,
)

ROUNDS = 4  # repetitions of the race tests; every round must pass


class TestRefreshGate:
    @pytest.mark.usefixtures("captured_emails")
    def test_refresh_with_reused_token_revokes_newest_token_too(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        r1, t1 = refresh_with(client, t0.refresh)  # legitimate rotation
        assert r1.status_code == 200
        assert t1 is not None

        # Replay of the rotated token = theft signal.
        replay, _ = refresh_with(client, t0.refresh)
        assert replay.status_code == 401

        # The newest token of the same family must now be dead, checked in isolation.
        newest, _ = refresh_with(client, t1.refresh)
        assert newest.status_code == 401
        assert active_session_count(db, "alice") == 0

    @pytest.mark.usefixtures("captured_emails")
    def test_reuse_detection_writes_session_revoked_audit_row(
        self, client: TestClient, db: Db
    ) -> None:
        """Audit side effect of reuse detection."""
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        refresh_with(client, t0.refresh)
        refresh_with(client, t0.refresh)

        detail = db.scalar(
            "SELECT detail FROM auth_audit_log WHERE lower(event_type::text) = "
            "'session_revoked' ORDER BY created_at DESC LIMIT 1"
        )
        assert detail
        assert "reuse" in detail.lower()

    @pytest.mark.usefixtures("captured_emails")
    def test_concurrent_refresh_with_same_cookie_yields_exactly_one_200(
        self, client: TestClient
    ) -> None:
        # Several rounds: the first round runs against cold connections and
        # requests may serialise by accident; from the second round on the
        # interleaving is reliable. Every round must have exactly one winner.
        register_ok(client, username="alice")
        for round_no in range(ROUNDS):
            t0 = login_tokens(client, "alice")

            def _refresh(_i: int, refresh: str = t0.refresh) -> Response:
                return refresh_with(client, refresh)[0]

            results = run_parallel(8, _refresh)

            statuses = sorted(r.status_code for r in results)
            assert statuses.count(200) == 1, (round_no, statuses)
            assert statuses.count(401) == 7, (round_no, statuses)

    @pytest.mark.usefixtures("captured_emails")
    def test_refresh_after_admin_revoke_returns_401(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        db.execute(
            "UPDATE sessions SET is_revoked = true, revoke_reason = 'admin', "
            "revoked_at = now()"
        )
        r, _ = refresh_with(client, t0.refresh)
        assert r.status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_refresh_of_expired_session_returns_401(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        db.execute("UPDATE sessions SET expires_at = now() - interval '1 minute'")
        r, _ = refresh_with(client, t0.refresh)
        assert r.status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_refresh_after_deactivate_returns_401(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        db.execute("UPDATE users SET is_active = false WHERE username = 'alice'")
        r, _ = refresh_with(client, t0.refresh)
        assert r.status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_refresh_after_admin_lock_returns_401_and_ends_the_session(
        self, client: TestClient, db: Db
    ) -> None:
        """Indefinite admin lock (locked_until IS NULL) kills the session."""
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL "
            "WHERE username = 'alice'"
        )
        r, _ = refresh_with(client, t0.refresh)
        assert r.status_code == 401
        assert active_session_count(db, "alice") == 0

    @pytest.mark.usefixtures("captured_emails")
    def test_refresh_under_timed_lock_still_works(
        self, client: TestClient, db: Db
    ) -> None:
        """A timed lock from failed logins blocks only the password login.

        Existing sessions keep refreshing.
        """
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, "
            "locked_until = now() + interval '1 hour' WHERE username = 'alice'"
        )
        r, t1 = refresh_with(client, t0.refresh)
        assert r.status_code == 200, r.text
        assert t1 is not None
        assert t1.refresh != t0.refresh
        assert active_session_count(db, "alice") == 1

    @pytest.mark.usefixtures("captured_emails")
    def test_valid_session_survives_redis_flush(
        self,
        client: TestClient,
        flush_redis: Callable[[], None],
    ) -> None:
        """Postgres, not Redis, is the source of truth for sessions."""
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        flush_redis()
        r, t1 = refresh_with(client, t0.refresh)
        assert r.status_code == 200, r.text
        assert t1 is not None
        assert t1.refresh != t0.refresh

    @pytest.mark.usefixtures("captured_emails")
    def test_raw_refresh_token_is_not_stored_in_redis(self, client: TestClient) -> None:
        """No raw refresh token may be a Redis key or value."""
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")

        assert_nothing_personal_in_redis(t0.refresh)
