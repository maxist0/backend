"""Expiry of every time-limited credential.

Two clocks, two mechanisms (do not mix them up):

* PYTHON clock (``datetime.now``/``time.time``): JWT ``exp``/``iat``, the
  ``expires_at`` values the service WRITES (``utcnow() + delta``) and the timed
  lock check (``lock_is_active``). ``time_machine.travel`` controls these.
* DATABASE clock (``now()`` inside SQL): the refresh rotation gate
  (``expires_at > now()``) and email-token redemption. time-machine cannot move
  it, so these checks are exercised by rewriting ``expires_at`` relative to the
  database's own ``now()`` through the ``db`` fixture, on both sides of the
  boundary.

Every test says which mechanism it uses. Frozen time (``tick=False``) makes the
arithmetic exact; destinations are whole seconds because JWT claims are.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import time_machine

from app.core.config import get_settings
from tests.helpers import (
    LOGIN_LOCKOUT_SECONDS,
    MAXIMUM_LOGIN_ATTEMPTS,
    PASSWORD,
    audit_count,
    bearer,
    login,
    login_tokens,
    refresh_with,
    register_ok,
)

RESET_TOKEN_LIFETIME = timedelta(hours=1)
VERIFY_TOKEN_LIFETIME = timedelta(hours=24)
SECOND = timedelta(seconds=1)


def _frozen_now() -> datetime:
    """Real "now" cut to whole seconds, as a travel destination."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def _travel(destination: datetime):
    return time_machine.travel(destination, tick=False)


def _access_lifetime() -> timedelta:
    return timedelta(minutes=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES)


def _refresh_lifetime() -> timedelta:
    return timedelta(days=get_settings().REFRESH_TOKEN_EXPIRE_DAYS)


def _alice(client) -> None:
    register_ok(client, username="alice")


class TestAccessTokenExpiry:
    """Access token (15 minutes) - Python clock, time-machine."""

    def test_exp_claim_is_issue_time_plus_lifetime(self, client, captured_emails):
        _alice(client)
        start = _frozen_now()

        with _travel(start):
            access = login_tokens(client, "alice").access

        claims = jwt.decode(access, options={"verify_signature": False})
        assert claims["iat"] == int(start.timestamp())
        assert claims["exp"] == int((start + _access_lifetime()).timestamp())

    def test_token_is_accepted_until_the_last_second_of_its_lifetime(
        self, client, captured_emails
    ):
        _alice(client)
        start = _frozen_now()
        with _travel(start):
            access = login_tokens(client, "alice").access

        with _travel(start + _access_lifetime() - SECOND):
            r = client.get("/api/v1/users/me", headers=bearer(access))

        assert r.status_code == 200

    def test_token_is_rejected_after_its_lifetime(self, client, captured_emails):
        _alice(client)
        start = _frozen_now()
        with _travel(start):
            access = login_tokens(client, "alice").access

        with _travel(start + _access_lifetime() + SECOND):
            r = client.get("/api/v1/users/me", headers=bearer(access))
            garbage = client.get("/api/v1/users/me", headers=bearer("not.a.jwt"))

        assert r.status_code == 401
        # An expired token is indistinguishable from an invalid one.
        assert (r.json()["code"], r.json()["detail"]) == (
            garbage.json()["code"],
            garbage.json()["detail"],
        )

    def test_refresh_after_access_expiry_issues_a_working_token(
        self, client, captured_emails
    ):
        """The access token expires, the 30-day refresh session does not."""
        _alice(client)
        start = _frozen_now()
        with _travel(start):
            tokens = login_tokens(client, "alice")

        later = start + _access_lifetime() + timedelta(minutes=1)
        with _travel(later):
            assert (
                client.get(
                    "/api/v1/users/me", headers=bearer(tokens.access)
                ).status_code
                == 401
            )
            r, renewed = refresh_with(client, tokens.refresh)
            assert r.status_code == 200
            assert renewed is not None
            assert (
                client.get(
                    "/api/v1/users/me", headers=bearer(renewed.access)
                ).status_code
                == 200
            )

        claims = jwt.decode(renewed.access, options={"verify_signature": False})
        assert claims["iat"] == int(later.timestamp())


class TestRefreshSessionLifetime:
    """Refresh session (REFRESH_TOKEN_EXPIRE_DAYS)."""

    def test_new_session_expires_after_the_configured_days(
        self, client, captured_emails, db
    ):
        """Python clock (time-machine): the value the service writes."""
        _alice(client)
        start = _frozen_now()

        with _travel(start):
            login_tokens(client, "alice")

        assert db.scalar("SELECT expires_at FROM sessions") == (
            start + _refresh_lifetime()
        )

    def test_rotation_grants_the_successor_a_full_new_lifetime(
        self, client, captured_emails, db
    ):
        """GUARD (characterisation): the lifetime slides, there is no absolute cap.

        Python clock (time-machine) for the written value; the rotation gate
        itself runs on the database clock and the old session is still valid
        there. If an absolute session lifetime is introduced, this test is the
        one to change.
        """
        _alice(client)
        start = _frozen_now()
        with _travel(start):
            tokens = login_tokens(client, "alice")

        later = start + timedelta(days=10)
        with _travel(later):
            r, _ = refresh_with(client, tokens.refresh)
        assert r.status_code == 200

        newest = db.scalar("SELECT max(expires_at) FROM sessions WHERE NOT is_revoked")
        assert newest == later + _refresh_lifetime()

    def test_session_is_refreshable_while_the_database_says_it_is_valid(
        self, client, captured_emails, db
    ):
        """Database clock (SQL rewrite): just inside the boundary."""
        _alice(client)
        tokens = login_tokens(client, "alice")
        db.execute("UPDATE sessions SET expires_at = now() + interval '1 minute'")

        r, renewed = refresh_with(client, tokens.refresh)

        assert r.status_code == 200
        assert renewed is not None

    def test_session_past_its_expiry_is_refused_and_not_rotated(
        self, client, captured_emails, db
    ):
        """Database clock (SQL rewrite): just outside the boundary."""
        _alice(client)
        tokens = login_tokens(client, "alice")
        db.execute("UPDATE sessions SET expires_at = now() - interval '1 second'")

        r, renewed = refresh_with(client, tokens.refresh)

        assert r.status_code == 401
        assert renewed is None
        row = db.one("SELECT is_revoked, revoke_reason FROM sessions")
        assert row["is_revoked"] is False  # expired, not "rotated": no reuse signal
        assert row["revoke_reason"] is None


def _request_reset(client, captured_emails) -> str:
    r = client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
    assert r.status_code == 202
    return captured_emails.latest_token("alice@example.com", "reset")


def _redeem_reset(client, token: str):
    return client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": "NewPassword456!"},
    )


def _redeem_verify(client, token: str):
    return client.post("/api/v1/auth/email/verify", json={"token": token})


def _token_row(db, purpose: str):
    return db.one(
        "SELECT expires_at, used_at FROM email_tokens "
        "WHERE lower(purpose::text) = :p AND invalidated_at IS NULL",
        p=purpose,
    )


class TestResetTokenLifetime:
    def test_token_expires_one_hour_after_the_request(
        self, client, captured_emails, db
    ):
        """Python clock (time-machine): the value the service writes."""
        _alice(client)
        start = _frozen_now()

        with _travel(start):
            _request_reset(client, captured_emails)

        assert _token_row(db, "reset_password")["expires_at"] == (
            start + RESET_TOKEN_LIFETIME
        )

    def test_token_is_redeemable_while_the_database_says_it_is_valid(
        self, client, captured_emails, db
    ):
        """Database clock (SQL rewrite): just inside the boundary."""
        _alice(client)
        token = _request_reset(client, captured_emails)
        db.execute("UPDATE email_tokens SET expires_at = now() + interval '1 minute'")

        assert _redeem_reset(client, token).status_code == 204

    def test_expired_token_is_refused_and_changes_nothing(
        self, client, captured_emails, db
    ):
        """Database clock (SQL rewrite): just outside the boundary."""
        _alice(client)
        token = _request_reset(client, captured_emails)
        db.execute("UPDATE email_tokens SET expires_at = now() - interval '1 second'")

        r = _redeem_reset(client, token)

        assert r.status_code == 400
        assert _token_row(db, "reset_password")["used_at"] is None
        assert login(client, username="alice", password=PASSWORD).status_code == 200
        assert (
            login(client, username="alice", password="NewPassword456!").status_code
            == 401
        )


class TestVerifyTokenLifetime:
    def test_registration_token_expires_24_hours_after_registration(
        self, client, captured_emails, db
    ):
        """Python clock (time-machine): the value the service writes."""
        start = _frozen_now()

        with _travel(start):
            _alice(client)

        assert _token_row(db, "verify_email")["expires_at"] == (
            start + VERIFY_TOKEN_LIFETIME
        )

    def test_resent_token_expires_24_hours_after_the_resend(
        self, client, captured_emails, db
    ):
        """Python clock (time-machine): the resend path has its own constant."""
        _alice(client)
        access = login_tokens(client, "alice").access
        start = _frozen_now()

        with _travel(start):
            r = client.post("/api/v1/auth/email/verify/resend", headers=bearer(access))
        assert r.status_code == 202

        assert _token_row(db, "verify_email")["expires_at"] == (
            start + VERIFY_TOKEN_LIFETIME
        )

    def test_token_is_redeemable_while_the_database_says_it_is_valid(
        self, client, captured_emails, db
    ):
        """Database clock (SQL rewrite): just inside the boundary."""
        _alice(client)
        token = captured_emails.latest_token("alice@example.com", "verify")
        db.execute("UPDATE email_tokens SET expires_at = now() + interval '1 minute'")

        assert _redeem_verify(client, token).status_code == 204

    def test_expired_token_is_refused_and_the_account_stays_unverified(
        self, client, captured_emails, db
    ):
        """Database clock (SQL rewrite): just outside the boundary."""
        _alice(client)
        token = captured_emails.latest_token("alice@example.com", "verify")
        db.execute("UPDATE email_tokens SET expires_at = now() - interval '1 second'")

        r = _redeem_verify(client, token)

        assert r.status_code == 400
        assert _token_row(db, "verify_email")["used_at"] is None
        assert db.scalar("SELECT is_email_verified FROM users") is False


class TestEmailChangeTokenLifetime:
    def _request_change(self, client, captured_emails) -> str:
        access = login_tokens(client, "alice").access
        r = client.put(
            "/api/v1/users/me/email",
            json={"current_password": PASSWORD, "new_email": "new@example.com"},
            headers=bearer(access),
        )
        assert r.status_code == 202, r.text
        return captured_emails.latest_token("new@example.com", "change_email")

    def test_token_expires_24_hours_after_the_request(
        self, client, captured_emails, db
    ):
        """Python clock (time-machine): the value the service writes."""
        _alice(client)
        start = _frozen_now()

        with _travel(start):
            self._request_change(client, captured_emails)

        assert _token_row(db, "change_email")["expires_at"] == (
            start + VERIFY_TOKEN_LIFETIME
        )

    def test_expired_token_is_refused_and_the_address_stays(
        self, client, captured_emails, db
    ):
        """Database clock (SQL rewrite): just outside the boundary."""
        _alice(client)
        token = self._request_change(client, captured_emails)
        db.execute(
            "UPDATE email_tokens SET expires_at = now() - interval '1 second' "
            "WHERE purpose::text = 'CHANGE_EMAIL'"
        )

        r = _redeem_verify(client, token)

        assert r.status_code == 400
        assert _token_row(db, "change_email")["used_at"] is None
        assert db.scalar("SELECT email FROM users") == "alice@example.com"


def _lock_alice(client, db) -> datetime:
    """Fail MAXIMUM_LOGIN_ATTEMPTS times; return the committed ``locked_until``.

    The destination for time travel comes from the database value, not from the
    host clock: a Docker host and its Postgres container can disagree by
    seconds.
    """
    for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
        login(client, username="alice", password="WrongPassword1!")
    row = db.one("SELECT is_locked, locked_until FROM users")
    assert row["is_locked"] is True
    return row["locked_until"]


class TestTimedLockEnd:
    def test_lock_lasts_the_configured_window(self, client, captured_emails, db):
        """Database clock at write time: locked_until = lock time + window."""
        _alice(client)
        _lock_alice(client, db)

        row = db.one("SELECT locked_until - updated_at AS span FROM users")

        assert row["span"] == timedelta(seconds=LOGIN_LOCKOUT_SECONDS)

    def test_login_is_refused_until_the_last_second_of_the_lock(
        self, client, captured_emails, db
    ):
        _alice(client)
        locked_until = _lock_alice(client, db)

        with _travel(locked_until - SECOND):
            r = login(client, username="alice", password=PASSWORD)

        assert r.status_code == 401
        assert r.json()["code"] == "invalid_credentials"
        assert db.scalar(
            "SELECT detail FROM auth_audit_log ORDER BY created_at DESC LIMIT 1"
        ).startswith("Account locked, ")
        assert db.scalar("SELECT is_locked FROM users") is True

    def test_login_works_again_after_the_lock_ended_and_is_audited(
        self, client, captured_emails, db
    ):
        _alice(client)
        locked_until = _lock_alice(client, db)

        with _travel(locked_until + SECOND):
            r = login(client, username="alice", password=PASSWORD)

        assert r.status_code == 200
        row = db.one("SELECT is_locked, locked_until, failed_login_attempts FROM users")
        assert row["is_locked"] is False
        assert row["locked_until"] is None
        assert row["failed_login_attempts"] == 0
        assert audit_count(db, "account_unlocked", "alice") == 1

    def test_failure_counter_restarts_after_the_lock_ended(
        self, client, captured_emails, db
    ):
        """One wrong guess after the lock must not re-lock immediately."""
        _alice(client)
        locked_until = _lock_alice(client, db)

        with _travel(locked_until + SECOND):
            r = login(client, username="alice", password="WrongPassword1!")

        assert r.status_code == 401
        row = db.one("SELECT is_locked, failed_login_attempts FROM users")
        assert row["is_locked"] is False
        assert row["failed_login_attempts"] == 1
