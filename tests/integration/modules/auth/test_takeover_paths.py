"""Account-takeover paths stay closed.

* A pending email change (or verification) dies with every event that
  should end an attacker's grip on the account: password change, password
  reset, admin lock and deactivation. A token is never applied to an
  inactive or admin-locked account.
* Logout with an already rotated refresh token is token reuse, exactly like
  a refresh with it; it must not overwrite the `rotated` marker.
* Wrong current passwords on `PUT /users/me/password` and
  `PUT /users/me/email` count towards the login lockout.
"""

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from tests.helpers import (
    MAXIMUM_LOGIN_ATTEMPTS,
    PASSWORD,
    CapturedEmails,
    Db,
    Tokens,
    admin_login,
    audit_count,
    bearer,
    change_email,
    change_password,
    login,
    login_tokens,
    refresh_with,
    register_ok,
    reset_password,
    run_action,
    user_id_of,
    verify_email,
)

NEW_EMAIL = "attacker@example.org"
NEW_PASSWORD = "NewPassword456!"


@pytest.fixture
def alice(client: TestClient, captured_emails: CapturedEmails) -> Tokens:
    """Alice, registered with her verification mail recorded, and logged in."""
    register_ok(client, username="alice")
    assert captured_emails.of_kind("verify")
    return login_tokens(client, "alice")


def _request_email_change(
    client: TestClient, captured_emails: CapturedEmails, access: str
) -> str:
    r = change_email(client, access, NEW_EMAIL)
    assert r.status_code == 202, r.text
    return captured_emails.latest_token(NEW_EMAIL, "change_email")


def _assert_refused_and_email_unchanged(response: Response, db: Db) -> None:
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_token"
    assert db.scalar("SELECT email FROM users") == "alice@example.com"


class TestPendingEmailChangeIsCancelled:
    def test_by_a_password_change(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db, alice: Tokens
    ) -> None:
        token = _request_email_change(client, captured_emails, alice.access)

        r = client.put(
            "/api/v1/users/me/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=bearer(alice.access),
        )
        assert r.status_code == 204

        _assert_refused_and_email_unchanged(verify_email(client, token), db)

    def test_by_a_password_reset(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db, alice: Tokens
    ) -> None:
        token = _request_email_change(client, captured_emails, alice.access)
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        reset = captured_emails.latest_token("alice@example.com", "reset")

        r = reset_password(client, reset, NEW_PASSWORD)
        assert r.status_code == 204

        _assert_refused_and_email_unchanged(verify_email(client, token), db)

    @pytest.mark.parametrize(
        ("block", "unblock"),
        [
            ("lock-users", "unlock-users"),
            ("deactivate-users", "reactivate-users"),
        ],
    )
    def test_by_an_admin_block_also_after_it_is_lifted(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        alice: Tokens,
        block: str,
        unblock: str,
    ) -> None:
        token = _request_email_change(client, captured_emails, alice.access)
        admin_login(client)
        run_action(client, "user", block, [user_id_of(db, "alice")])
        run_action(client, "user", unblock, [user_id_of(db, "alice")])

        _assert_refused_and_email_unchanged(verify_email(client, token), db)
        assert (
            db.scalar("SELECT count(*) FROM email_tokens WHERE invalidated_at IS NULL")
            == 0
        )

    @pytest.mark.parametrize(
        "block_sql",
        [
            "UPDATE users SET is_active = false",
            "UPDATE users SET is_locked = true, locked_until = NULL",
        ],
        ids=["inactive", "admin_locked"],
    )
    def test_no_token_applies_to_a_blocked_account(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        alice: Tokens,
        block_sql: str,
    ) -> None:
        change = _request_email_change(client, captured_emails, alice.access)
        verify = captured_emails.latest_token("alice@example.com", "verify")
        db.execute(block_sql)

        _assert_refused_and_email_unchanged(verify_email(client, change), db)
        r = verify_email(client, verify)
        assert r.status_code == 400
        assert r.json()["code"] == "invalid_token"
        assert db.scalar("SELECT is_email_verified FROM users") is False

        # The refused tokens are spent: unblocking does not revive them.
        db.execute(
            "UPDATE users SET is_active = true, is_locked = false, locked_until = NULL"
        )
        _assert_refused_and_email_unchanged(verify_email(client, change), db)
        assert verify_email(client, verify).status_code == 400


class TestLogoutWithRotatedToken:
    def test_is_reuse_keeps_the_marker_and_ends_the_family(
        self, client: TestClient, db: Db, alice: Tokens
    ) -> None:
        _, successor = refresh_with(client, alice.refresh)

        r = client.post(
            "/api/v1/auth/logout", headers={"Cookie": f"refresh_token={alice.refresh}"}
        )

        assert r.status_code == 401
        assert r.json()["code"] == "invalid_refresh_token"
        assert 'refresh_token=""' in r.headers["set-cookie"]
        assert (
            db.scalar("SELECT revoke_reason FROM sessions ORDER BY created_at LIMIT 1")
            == "rotated"
        )
        assert db.scalar("SELECT count(*) FROM sessions WHERE NOT is_revoked") == 0
        assert successor
        assert refresh_with(client, successor.refresh)[0].status_code == 401
        assert audit_count(db, "session_revoked", "alice") == 1

    def test_second_logout_is_idempotent_and_keeps_the_reason(
        self, client: TestClient, db: Db, alice: Tokens
    ) -> None:
        cookie = {"Cookie": f"refresh_token={alice.refresh}"}
        assert client.post("/api/v1/auth/logout", headers=cookie).status_code == 204
        db.execute("UPDATE sessions SET revoke_reason = 'admin'")

        r = client.post("/api/v1/auth/logout", headers=cookie)

        assert r.status_code == 204
        assert db.scalar("SELECT revoke_reason FROM sessions") == "admin"


def _change_password(client: TestClient, access: str, current: str) -> Response:
    return change_password(client, access, current, NEW_PASSWORD)


def _change_email(client: TestClient, access: str, current: str) -> Response:
    return change_email(client, access, NEW_EMAIL, current)


class TestPasswordConfirmationLockout:
    # Many requests from one client: this tests the mechanism behind the rate
    # limits, which must also hold while they are off (fail-open).
    @pytest.mark.usefixtures("captured_emails", "without_rate_limits")
    @pytest.mark.parametrize("confirm", [_change_password, _change_email])
    def test_wrong_passwords_lock_the_account_like_failed_logins(
        self,
        client: TestClient,
        db: Db,
        alice: Tokens,
        confirm: Callable[[TestClient, str, str], Response],
    ) -> None:
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            r = confirm(client, alice.access, "WrongPassword1!")
            assert r.status_code == 400
            assert r.json()["code"] == "wrong_password"

        row = db.one("SELECT is_locked, locked_until, failed_login_attempts FROM users")
        assert row["is_locked"] is True
        assert row["locked_until"] is not None, "a timed lock, as after failed logins"
        assert row["failed_login_attempts"] == MAXIMUM_LOGIN_ATTEMPTS
        assert audit_count(db, "account_locked", "alice") == 1
        assert audit_count(db, "login_failed", "alice") == MAXIMUM_LOGIN_ATTEMPTS - 1

        # Locked: the correct password is refused too, here and at login.
        assert confirm(client, alice.access, PASSWORD).status_code == 400
        assert (
            db.scalar(
                "SELECT count(*) FROM auth_audit_log "
                "WHERE detail = 'Password confirmation, account locked'"
            )
            == 1
        )
        assert login(client, username="alice").status_code == 401
        # Like a login lockout, the timed lock does not end the sessions.
        assert (
            client.get("/api/v1/users/me", headers=bearer(alice.access)).status_code
            == 200
        )

    def test_correct_password_before_the_threshold_works_and_resets_the_count(
        self, client: TestClient, db: Db, alice: Tokens
    ) -> None:
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS - 1):
            assert (
                _change_password(client, alice.access, "WrongPassword1!").status_code
                == 400
            )

        r = _change_password(client, alice.access, PASSWORD)

        assert r.status_code == 204
        assert db.scalar("SELECT failed_login_attempts FROM users") == 0
        assert db.scalar("SELECT is_locked FROM users") is False
