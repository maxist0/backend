"""`Idempotency-Key` on the built-in routes of `auth` and `users`.

Each marked route answers a retry with the stored response and runs
nothing a second time: no second account, mail, token or audit row. The
mechanism itself (parallel requests, crash window, header format) is tested
with probe routes in `tests/integration/core/test_idempotency.py`.
"""

from datetime import UTC, datetime, timedelta

import pytest
import time_machine
from fastapi.testclient import TestClient

from tests.helpers import (
    NEW_PASSWORD,
    PASSWORD,
    CapturedEmails,
    Db,
    assert_rate_limited,
    assert_replayed,
    audit_count,
    bearer,
    idempotency_rows,
    keyed,
    login_tokens,
    new_idempotency_key,
    refresh_with,
    register_ok,
    run_parallel,
    user_id_of,
)

REGISTER = "/api/v1/auth/register"
FORGOT = "/api/v1/auth/password/forgot"
RESET = "/api/v1/auth/password/reset"
VERIFY = "/api/v1/auth/email/verify"
RESEND = "/api/v1/auth/email/verify/resend"
ME = "/api/v1/users/me"
MY_PASSWORD = "/api/v1/users/me/password"
MY_EMAIL = "/api/v1/users/me/email"

ALICE = {"username": "alice", "email": "alice@example.com", "password": PASSWORD}


def _user_count(db: Db) -> int:
    return int(db.scalar("SELECT count(*) FROM users"))


def _access(client: TestClient) -> str:
    register_ok(client, username="alice")
    return login_tokens(client, "alice").access


type Outcome = tuple[int, str | None, bool]


def _forgot_pair(client: TestClient, email: str) -> tuple[Outcome, ...]:
    """Two parallel forgot-password requests with one key: (status, code, replayed)."""
    key = new_idempotency_key()
    pair = run_parallel(
        2, lambda _i: client.post(FORGOT, json={"email": email}, headers=keyed(key))
    )
    return tuple(
        sorted(
            (
                r.status_code,
                r.json()["code"] if r.status_code >= 400 else None,
                "Idempotent-Replayed" in r.headers,
            )
            for r in pair
        )
    )


class TestRegister:
    def test_a_retry_replays_the_201_and_creates_no_second_account(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        key = new_idempotency_key()

        first = client.post(REGISTER, json=ALICE, headers=keyed(key))
        retry = client.post(REGISTER, json=ALICE, headers=keyed(key))

        assert first.status_code == 201
        assert first.json()["username"] == "alice"
        assert_replayed(first, retry)
        assert _user_count(db) == 1
        assert len(captured_emails.of_kind("verify")) == 1
        (row,) = idempotency_rows(db, key)
        assert row["scope"] == "route:POST /api/v1/auth/register"

    @pytest.mark.parametrize("attempt", range(3))
    @pytest.mark.usefixtures("without_rate_limits")
    def test_a_parallel_burst_with_one_key_registers_once(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails, attempt: int
    ) -> None:
        key = new_idempotency_key()

        responses = run_parallel(
            6, lambda _i: client.post(REGISTER, json=ALICE, headers=keyed(key))
        )

        assert _user_count(db) == 1, attempt
        assert len(captured_emails.of_kind("verify")) == 1
        created = [r for r in responses if r.status_code == 201]
        assert sum("Idempotent-Replayed" not in r.headers for r in created) == 1
        assert {r.status_code for r in responses} <= {201, 409}
        assert all(
            r.json()["code"] == "idempotency_key_in_use"
            for r in responses
            if r.status_code == 409
        )

    def test_a_refused_registration_leaves_no_key(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        key = new_idempotency_key()

        first = client.post(REGISTER, json=ALICE, headers=keyed(key))
        retry = client.post(REGISTER, json=ALICE, headers=keyed(key))

        assert first.status_code == retry.status_code == 409
        assert retry.json()["code"] == "user_exists"
        assert "Idempotent-Replayed" not in retry.headers
        assert idempotency_rows(db) == []
        assert len(captured_emails) == 1

    def test_another_body_with_the_same_key_gets_422(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        key = new_idempotency_key()
        client.post(REGISTER, json=ALICE, headers=keyed(key))

        r = client.post(
            REGISTER,
            json={**ALICE, "username": "mallory", "email": "m@example.com"},
            headers=keyed(key),
        )

        assert r.status_code == 422
        assert r.json()["code"] == "idempotency_key_reused"
        assert len(captured_emails) == 1


class TestForgotPassword:
    def test_a_retry_sends_no_second_mail(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        key = new_idempotency_key()
        body = {"email": "alice@example.com"}

        first = client.post(FORGOT, json=body, headers=keyed(key))
        retry = client.post(FORGOT, json=body, headers=keyed(key))

        assert first.status_code == 202
        assert_replayed(first, retry)
        assert len(captured_emails.of_kind("reset")) == 1

    def test_an_unknown_address_is_answered_the_same_and_replayed(
        self, client: TestClient, db: Db
    ) -> None:
        key = new_idempotency_key()
        body = {"email": "nobody@example.com"}

        first = client.post(FORGOT, json=body, headers=keyed(key))
        retry = client.post(FORGOT, json=body, headers=keyed(key))

        assert first.status_code == 202
        assert_replayed(first, retry)
        (row,) = idempotency_rows(db, key)
        assert row["response_status"] == 202

    @pytest.mark.usefixtures("without_rate_limits", "captured_emails")
    def test_a_parallel_pair_answers_alike_for_known_and_unknown_addresses(
        self, client: TestClient
    ) -> None:
        """The commit point must not tell whether an address is registered.

        A parallel request with the same key sees the first one's key row at
        its commit. If an unknown address committed later than a known one
        (together with the stored response), the pair would get a replay for
        unknown and 409 for known addresses.
        """
        register_ok(client, username="alice")

        known = {_forgot_pair(client, "alice@example.com") for _ in range(6)}
        unknown = {_forgot_pair(client, "nobody@example.com") for _ in range(6)}

        assert known == unknown


class TestResetPassword:
    def test_a_retry_after_the_reset_replays_204_instead_of_invalid_token(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        client.post(FORGOT, json={"email": "alice@example.com"})
        token = captured_emails.latest_token("alice@example.com", "reset")
        key = new_idempotency_key()
        body = {"token": token, "new_password": NEW_PASSWORD}

        first = client.post(RESET, json=body, headers=keyed(key))
        retry = client.post(RESET, json=body, headers=keyed(key))
        without_key = client.post(RESET, json=body)

        assert first.status_code == 204
        assert_replayed(first, retry)
        assert without_key.status_code == 400
        assert audit_count(db, "password_reset_completed", "alice") == 1

    def test_an_invalid_token_leaves_no_key(self, client: TestClient, db: Db) -> None:
        key = new_idempotency_key()
        body = {"token": "x" * 43, "new_password": NEW_PASSWORD}

        r = client.post(RESET, json=body, headers=keyed(key))

        assert r.status_code == 400
        assert r.json()["code"] == "invalid_token"
        assert idempotency_rows(db) == []


class TestVerifyEmail:
    def test_a_retry_after_the_verification_replays_204(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        token = captured_emails.latest_token("alice@example.com", "verify")
        key = new_idempotency_key()

        first = client.post(VERIFY, json={"token": token}, headers=keyed(key))
        retry = client.post(VERIFY, json={"token": token}, headers=keyed(key))

        assert first.status_code == 204
        assert_replayed(first, retry)
        assert audit_count(db, "email_verified", "alice") == 1

    def test_a_token_of_a_blocked_account_is_spent_and_the_400_replayed(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        token = captured_emails.latest_token("alice@example.com", "verify")
        db.execute("UPDATE users SET is_active = false")
        key = new_idempotency_key()

        first = client.post(VERIFY, json={"token": token}, headers=keyed(key))
        retry = client.post(VERIFY, json={"token": token}, headers=keyed(key))

        assert first.status_code == 400
        assert first.json()["code"] == "invalid_token"
        assert_replayed(first, retry)


class TestResendVerification:
    def test_a_retry_sends_no_second_mail(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        access = _access(client)
        key = new_idempotency_key()

        first = client.post(RESEND, headers=keyed(key, bearer(access)))
        retry = client.post(RESEND, headers=keyed(key, bearer(access)))

        assert first.status_code == 202
        assert_replayed(first, retry)
        assert len(captured_emails.of_kind("verify")) == 2  # registration + one


class TestUpdateProfile:
    def test_a_retry_replays_the_200(self, client: TestClient, db: Db) -> None:
        access = _access(client)
        key = new_idempotency_key()
        body = {"display_name": "Alice"}

        first = client.patch(ME, json=body, headers=keyed(key, bearer(access)))
        retry = client.patch(ME, json=body, headers=keyed(key, bearer(access)))

        assert first.status_code == 200
        assert first.json()["display_name"] == "Alice"
        assert_replayed(first, retry)
        (row,) = idempotency_rows(db, key)
        assert row["scope"] == f"user:{user_id_of(db, 'alice')}"

    def test_keyed_updates_beyond_the_limit_get_429_and_leave_no_key(
        self, client: TestClient, db: Db
    ) -> None:
        access = _access(client)
        for i in range(30):
            r = client.patch(ME, json={"display_name": f"N{i}"}, headers=bearer(access))
            assert r.status_code == 200, r.text

        r = client.patch(
            ME,
            json={"display_name": "Over"},
            headers=keyed(new_idempotency_key(), bearer(access)),
        )

        assert_rate_limited(r, window=60)
        assert idempotency_rows(db) == []

    def test_a_request_without_token_gets_401_and_leaves_no_key(
        self, client: TestClient, db: Db
    ) -> None:
        r = client.patch(
            ME, json={"display_name": "A"}, headers=keyed(new_idempotency_key())
        )

        assert r.status_code == 401
        assert idempotency_rows(db) == []


class TestChangePassword:
    def test_after_the_change_the_retry_needs_a_refresh_then_replays(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        # An older `iat`: a token of the change's own second stays valid.
        with time_machine.travel(datetime.now(UTC) - timedelta(seconds=10)):
            tokens = login_tokens(client, "alice")
        key = new_idempotency_key()
        body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}

        first = client.put(
            MY_PASSWORD, json=body, headers=keyed(key, bearer(tokens.access))
        )
        with_old_token = client.put(
            MY_PASSWORD, json=body, headers=keyed(key, bearer(tokens.access))
        )
        _, refreshed = refresh_with(client, tokens.refresh)
        assert refreshed is not None
        retry = client.put(
            MY_PASSWORD, json=body, headers=keyed(key, bearer(refreshed.access))
        )

        assert first.status_code == 204
        assert with_old_token.status_code == 401
        assert_replayed(first, retry)
        assert audit_count(db, "password_changed", "alice") == 1

    def test_a_wrong_password_is_replayed_and_counted_once(
        self, client: TestClient, db: Db
    ) -> None:
        access = _access(client)
        key = new_idempotency_key()
        body = {"current_password": "Wrong-password-1", "new_password": NEW_PASSWORD}

        first = client.put(MY_PASSWORD, json=body, headers=keyed(key, bearer(access)))
        retry = client.put(MY_PASSWORD, json=body, headers=keyed(key, bearer(access)))

        assert first.status_code == 400
        assert first.json()["code"] == "wrong_password"
        assert_replayed(first, retry)
        attempts = db.scalar("SELECT failed_login_attempts FROM users")
        assert attempts == 1


class TestChangeEmail:
    def test_a_retry_sends_no_second_link(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        access = _access(client)
        key = new_idempotency_key()
        body = {"current_password": PASSWORD, "new_email": "new@example.com"}

        first = client.put(MY_EMAIL, json=body, headers=keyed(key, bearer(access)))
        retry = client.put(MY_EMAIL, json=body, headers=keyed(key, bearer(access)))

        assert first.status_code == 202
        assert_replayed(first, retry)
        assert len(captured_emails.of_kind("change_email")) == 1

    def test_a_recipient_limit_after_the_confirmation_deletes_the_key(
        self, client: TestClient, db: Db
    ) -> None:
        """The 429 comes after the password confirmation committed the key row."""
        access = _access(client)
        new_email = {"current_password": PASSWORD, "new_email": "new@example.com"}
        for _ in range(3):
            client.put(MY_EMAIL, json=new_email, headers=bearer(access))
        key = new_idempotency_key()

        first = client.put(MY_EMAIL, json=new_email, headers=keyed(key, bearer(access)))
        retry = client.put(MY_EMAIL, json=new_email, headers=keyed(key, bearer(access)))

        assert first.status_code == retry.status_code == 429
        assert "Idempotent-Replayed" not in retry.headers
        assert idempotency_rows(db) == []


@pytest.mark.usefixtures("captured_emails")
def test_no_marked_route_stores_a_cookie_or_a_token(client: TestClient, db: Db) -> None:
    """Every stored response of the built-in routes is free of credentials."""
    access = _access(client)
    headers = bearer(access)
    client.patch(
        ME, json={"display_name": "A"}, headers=keyed(new_idempotency_key(), headers)
    )
    client.post(RESEND, headers=keyed(new_idempotency_key(), headers))
    client.post(
        FORGOT,
        json={"email": "alice@example.com"},
        headers=keyed(new_idempotency_key()),
    )

    rows = idempotency_rows(db)
    assert len(rows) == 3
    for row in rows:
        names = {name.lower() for name, _ in row["response_headers"]}
        assert "set-cookie" not in names
        assert b"access_token" not in bytes(row["response_body"])
        assert b"refresh_token" not in bytes(row["response_body"])
