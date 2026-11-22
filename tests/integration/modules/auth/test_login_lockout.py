"""Failed logins: every refusal looks the same, and the lockout.

A timed lock from failed logins blocks only the password login; existing
sessions and access tokens survive it. Only the indefinite admin lock
(`is_locked` with `locked_until` NULL) ends sessions and tokens.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from app.core.security import login_admission_limiter
from app.modules.auth import service
from tests.helpers import (
    MAXIMUM_LOGIN_ATTEMPTS,
    Db,
    active_session_count,
    bearer,
    login,
    login_tokens,
    refresh_with,
    register_ok,
    run_parallel,
)

# Any: the problem document as the client parses it.
type _Refusal = tuple[int, str | None, str | None, str | None, dict[str, Any]]


def _refusal(response: Response) -> _Refusal:
    """Everything a client can read from a refused login, except `request_id`."""
    body: dict[str, Any] = response.json()
    body.pop("request_id", None)
    return (
        response.status_code,
        response.headers.get("content-type"),
        response.headers.get("www-authenticate"),
        response.headers.get("retry-after"),
        body,
    )


_INVALID_CREDENTIALS: _Refusal = (
    401,
    "application/problem+json",
    "Bearer",
    None,
    {
        "type": "about:blank",
        "title": "Unauthorized",
        "status": 401,
        "detail": "Invalid username or password",
        "code": "invalid_credentials",
    },
)


class TestUniformLoginRefusal:
    """Every refused login looks the same.

    The answer reveals neither whether the account exists nor whether it is
    locked or deactivated.
    """

    def test_unknown_user(self, client: TestClient) -> None:
        assert _refusal(login(client, username="ghost")) == _INVALID_CREDENTIALS

    @pytest.mark.usefixtures("captured_emails")
    def test_wrong_password(self, client: TestClient) -> None:
        register_ok(client, username="alice")
        r = login(client, username="alice", password="WrongPass!1")
        assert _refusal(r) == _INVALID_CREDENTIALS

    @pytest.mark.usefixtures("captured_emails")
    def test_threshold_guess_and_every_guess_while_timed_locked(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            r = login(client, username="alice", password="WrongPass!1")
            assert _refusal(r) == _INVALID_CREDENTIALS  # the last one locks
        row = db.one("SELECT is_locked, locked_until FROM users")
        assert row["is_locked"] is True
        assert row["locked_until"] is not None

        wrong = login(client, username="alice", password="WrongPass!1")
        correct = login(client, username="alice")

        assert _refusal(wrong) == _INVALID_CREDENTIALS
        assert _refusal(correct) == _INVALID_CREDENTIALS
        assert "refresh_token" not in client.cookies

    @pytest.mark.usefixtures("captured_emails")
    def test_correct_password_while_admin_locked(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        db.execute("UPDATE users SET is_locked = true, locked_until = NULL")

        assert _refusal(login(client, username="alice")) == _INVALID_CREDENTIALS

    @pytest.mark.usefixtures("captured_emails")
    def test_correct_password_of_inactive_account(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        db.execute("UPDATE users SET is_active = false")

        assert _refusal(login(client, username="alice")) == _INVALID_CREDENTIALS

    @pytest.mark.usefixtures("captured_emails")
    @pytest.mark.parametrize("path", ["first_check", "recheck_after_admission"])
    def test_locked_refusal_waits_instead_of_running_argon2(
        self,
        client: TestClient,
        db: Db,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
    ) -> None:
        """The timing wait replaces Argon2 on locked accounts.

        It runs outside the admission limiter (it holds no slot).
        """
        register_ok(client, username="alice")
        if path == "first_check":
            db.execute(
                "UPDATE users SET is_locked = true, "
                "locked_until = now() + interval '1 hour'"
            )
        else:
            # The account looks unlocked at the first check and locked at the
            # re-check, as when a parallel guess locked it meanwhile.
            def _locked(*_args: object) -> bool:
                return True

            monkeypatch.setattr(service, "lock_is_active", _locked)
        calls: list[tuple[str, float]] = []

        async def wait() -> None:
            calls.append(("wait", login_admission_limiter().borrowed_tokens))

        async def argon2(*_args: object) -> bool:
            calls.append(("argon2", 0))
            return False

        monkeypatch.setattr(service, "wait_like_password_verify", wait)
        monkeypatch.setattr(service, "verify_password", argon2)
        monkeypatch.setattr(service, "equalize_password_timing", argon2)

        r = login(client, username="alice")

        assert _refusal(r) == _INVALID_CREDENTIALS
        assert calls == [("wait", 0)]

    @pytest.mark.usefixtures("captured_emails")
    def test_lockout_audit_rows_are_unchanged(self, client: TestClient, db: Db) -> None:
        """Failures, the lock and refused logins on a locked account are audited.

        Four failures, the locking fifth, then two refused logins on the locked
        account (wrong and correct password).
        """
        register_ok(client, username="alice")
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            login(client, username="alice", password="WrongPass!1")
        login(client, username="alice", password="WrongPass!1")
        login(client, username="alice")

        rows = db.rows(
            "SELECT event_type::text AS event, detail FROM auth_audit_log "
            "WHERE event_type::text IN ('LOGIN_FAILED', 'ACCOUNT_LOCKED') "
            "ORDER BY created_at"
        )
        events = [r["event"] for r in rows]
        assert (
            events == ["LOGIN_FAILED"] * 4 + ["ACCOUNT_LOCKED"] + ["LOGIN_FAILED"] * 2
        )
        assert (
            rows[4]["detail"]
            == f"Locked after {MAXIMUM_LOGIN_ATTEMPTS} failed attempts"
        )
        assert all(r["detail"].startswith("Account locked, ") for r in rows[5:])
        row = db.one("SELECT failed_login_attempts, is_locked FROM users")
        # Guesses on a locked account are refused before they are counted.
        assert row["failed_login_attempts"] == MAXIMUM_LOGIN_ATTEMPTS
        assert row["is_locked"] is True


class TestLockout:
    @pytest.mark.usefixtures("captured_emails")
    def test_parallel_failed_logins_lock_the_account(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")

        results = run_parallel(
            20, lambda _i: login(client, username="alice", password="WrongPass!1")
        )
        assert all(r.status_code == 401 for r in results)

        row = db.one(
            "SELECT failed_login_attempts, is_locked FROM users WHERE username='alice'"
        )
        assert row["failed_login_attempts"] >= MAXIMUM_LOGIN_ATTEMPTS, dict(row)
        assert row["is_locked"] is True
        # ...and the correct password no longer works.
        assert login(client, username="alice").status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_sequential_failed_logins_lock_account_at_threshold(
        self, client: TestClient, db: Db
    ) -> None:
        """The single-threaded semantics stay as they are."""
        register_ok(client, username="alice")
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            login(client, username="alice", password="WrongPass!1")
        row = db.one("SELECT failed_login_attempts, is_locked FROM users")
        assert row["failed_login_attempts"] == MAXIMUM_LOGIN_ATTEMPTS
        assert row["is_locked"] is True

    @pytest.mark.usefixtures("captured_emails")
    def test_admin_lock_without_locked_until_survives_login_attempt(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL "
            "WHERE username = 'alice'"
        )

        assert login(client, username="alice").status_code == 401
        assert login(client, username="alice").status_code == 401

        row = db.one("SELECT is_locked, locked_until FROM users")
        assert row["is_locked"] is True
        assert row["locked_until"] is None

    @pytest.mark.usefixtures("captured_emails")
    def test_expired_timed_lock_is_lifted_by_successful_login(
        self, client: TestClient, db: Db
    ) -> None:
        """Auto-unlock applies only when locked_until is set and past."""
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET is_locked = true, failed_login_attempts = 5, "
            "locked_until = now() - interval '1 minute' WHERE username = 'alice'"
        )
        assert login(client, username="alice").status_code == 200
        row = db.one("SELECT is_locked, failed_login_attempts FROM users")
        assert row["is_locked"] is False
        assert row["failed_login_attempts"] == 0

    @pytest.mark.usefixtures("captured_emails")
    def test_access_token_of_admin_locked_user_is_rejected(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        assert (
            client.get("/api/v1/users/me", headers=bearer(t.access)).status_code == 200
        )

        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL "
            "WHERE username = 'alice'"
        )

        assert (
            client.get("/api/v1/users/me", headers=bearer(t.access)).status_code == 401
        )

    @pytest.mark.usefixtures("captured_emails")
    def test_access_token_under_timed_lock_is_still_accepted(
        self, client: TestClient, db: Db
    ) -> None:
        """The timed (brute-force) lock does not touch access tokens."""
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, "
            "locked_until = now() + interval '1 hour' WHERE username = 'alice'"
        )
        assert (
            client.get("/api/v1/users/me", headers=bearer(t.access)).status_code == 200
        )

    @pytest.mark.usefixtures("captured_emails")
    def test_failed_logins_lock_only_the_password_login(
        self, client: TestClient, db: Db
    ) -> None:
        """Guessing locks only the password login; the real user stays logged in.

        Session, access token and refresh survive; only a new password login
        is refused.
        """
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")

        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            assert (
                login(client, username="alice", password="WrongPass!1").status_code
                == 401
            )
        client.cookies.clear()
        row = db.one("SELECT is_locked, locked_until FROM users")
        assert row["is_locked"] is True
        assert row["locked_until"] is not None

        assert login(client, username="alice").status_code == 401  # new login: locked
        client.cookies.clear()
        assert (
            client.get("/api/v1/users/me", headers=bearer(t.access)).status_code == 200
        )
        r, t1 = refresh_with(client, t.refresh)
        assert r.status_code == 200, r.text
        assert t1
        assert (
            client.get("/api/v1/users/me", headers=bearer(t1.access)).status_code == 200
        )
        assert active_session_count(db, "alice") == 1

    @pytest.mark.usefixtures("captured_emails")
    def test_admin_lock_invalidates_sessions_that_were_valid_before(
        self, client: TestClient, db: Db
    ) -> None:
        """Control for the test above: the indefinite lock still ends everything."""
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL "
            "WHERE username = 'alice'"
        )
        assert (
            client.get("/api/v1/users/me", headers=bearer(t.access)).status_code == 401
        )
        assert refresh_with(client, t.refresh)[0].status_code == 401
        assert login(client, username="alice").status_code == 401
        client.cookies.clear()

    @pytest.mark.usefixtures("captured_emails")
    def test_access_token_of_deactivated_user_is_rejected(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        db.execute("UPDATE users SET is_active = false WHERE username = 'alice'")
        assert (
            client.get("/api/v1/users/me", headers=bearer(t.access)).status_code == 401
        )
