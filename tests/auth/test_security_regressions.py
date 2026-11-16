# tests/auth/test_security_regressions.py
"""
Phase 0 security regression tests (review 2026-11). RED before the Step C fixes.

Mapping tests -> findings (IDs from documentation/review-2026-11):

  TestRefreshGate ............ K1/H1: SEC-02, SEC-03, BE-01, BE-02, DB-01, DB-03,
                               DB-10, BE-32, TST-12 (Postgres is the single atomic gate)
  TestSessionRevocation ...... H2 / Known issue 5: SEC-04 (reset, change password,
                               logout-all, sid claim)
  TestLockout ................ H4: SEC-07, BE-09, DB-04; H5 (admin lock): SEC-09, DB-05
  TestEmailTokensSingleUse ... M3: SEC-19, BE-08, DB-15
  TestJwtValidation .......... H16: SEC-16, BE-21, TST-13
  TestInputBounds ............ H8: DB-07, SEC-17; M9: SEC-18, SEC-20, BE-19
  TestLoggingHygiene ......... H9: OBS-02..05, OBS-22/23, SEC-12 (auth/email logs);
                               K3: OBS-01, SEC-05, BE-06, DB-09 (guard, fixed in step A)

Phase 0b (re-audit): the lock semantics follow the user decision NEW-3 (a timed lock
blocks only the password login, only the indefinite admin lock ends sessions/tokens);
concurrency findings NEW-1/2/4/5 live in tests/auth/test_concurrency.py.

Tests marked "GUARD" are expected GREEN already: they pin behaviour that must
survive the fix (or prove the RED tests fail for the right reason).

Target contract assumed by these tests:
  - POST /auth/logout-all  (Bearer; 200; deletes refresh cookie; audit row; 401 w/o token)
  - access token carries claim `sid` == sessions.family_id (stable across rotation)
  - a user with is_locked=true and locked_until NULL is locked indefinitely
"""

import logging

import jwt
import pytest

from app.common.constants import MAXIMUM_LOGIN_ATTEMPTS
from tests.helpers import (
    PASSWORD,
    active_session_count,
    audit_count,
    bearer,
    forge_access_token,
    login,
    login_tokens,
    refresh_with,
    register,
    run_parallel,
    secret_value,
    user_id_of,
)


ROUNDS = 4  # repetitions for race tests (see test_concurrent_refresh...)


def _alice(client):
    assert register(client, username="alice").status_code == 201


def _reset_password(client, captured_emails, new_password="NewPassword456!"):
    client.post("/auth/password/forgot", json={"email": "alice@example.com"})
    token = captured_emails.latest_token("alice@example.com", "reset")
    return client.post(
        "/auth/password/reset", json={"token": token, "new_password": new_password}
    )


# ---------------------------------------------------------------------------
# Refresh gate (K1 / H1)
# ---------------------------------------------------------------------------


class TestRefreshGate:
    def test_refresh_with_reused_token_revokes_newest_token_too(
        self, client, captured_emails, db
    ):
        _alice(client)
        t0 = login_tokens(client, "alice")
        r1, t1 = refresh_with(client, t0.refresh)  # legitimate rotation
        assert r1.status_code == 200 and t1 is not None

        # Replay of the rotated token = theft signal.
        replay, _ = refresh_with(client, t0.refresh)
        assert replay.status_code == 401

        # The newest token of the same family must now be dead, checked in isolation.
        newest, _ = refresh_with(client, t1.refresh)
        assert newest.status_code == 401
        assert active_session_count(db, "alice") == 0

    def test_reuse_detection_writes_session_revoked_audit_row(
        self, client, captured_emails, db
    ):
        """GUARD: audit side effect of reuse detection."""
        _alice(client)
        t0 = login_tokens(client, "alice")
        refresh_with(client, t0.refresh)
        refresh_with(client, t0.refresh)

        detail = db.scalar(
            "SELECT detail FROM auth_audit_log WHERE lower(event_type::text) = "
            "'session_revoked' ORDER BY created_at DESC LIMIT 1"
        )
        assert detail and "reuse" in detail.lower()

    def test_concurrent_refresh_with_same_cookie_yields_exactly_one_200(
        self, client, captured_emails, db
    ):
        # Several rounds: the first round runs against cold connections and
        # requests may serialise by accident; from the second round on the
        # interleaving is reliable. Every round must have exactly one winner.
        _alice(client)
        for round_no in range(ROUNDS):
            t0 = login_tokens(client, "alice")

            results = run_parallel(8, lambda _i: refresh_with(client, t0.refresh)[0])

            statuses = sorted(r.status_code for r in results)
            assert statuses.count(200) == 1, (round_no, statuses)
            assert statuses.count(401) == 7, (round_no, statuses)

    def test_refresh_after_admin_revoke_returns_401(self, client, captured_emails, db):
        _alice(client)
        t0 = login_tokens(client, "alice")
        db.execute(
            "UPDATE sessions SET is_revoked = true, revoke_reason = 'admin', "
            "revoked_at = now()"
        )
        r, _ = refresh_with(client, t0.refresh)
        assert r.status_code == 401

    def test_refresh_of_expired_session_returns_401(self, client, captured_emails, db):
        _alice(client)
        t0 = login_tokens(client, "alice")
        db.execute("UPDATE sessions SET expires_at = now() - interval '1 minute'")
        r, _ = refresh_with(client, t0.refresh)
        assert r.status_code == 401

    def test_refresh_after_deactivate_returns_401(self, client, captured_emails, db):
        _alice(client)
        t0 = login_tokens(client, "alice")
        db.execute("UPDATE users SET is_active = false WHERE username = 'alice'")
        r, _ = refresh_with(client, t0.refresh)
        assert r.status_code == 401

    def test_refresh_after_admin_lock_returns_401_and_ends_the_session(
        self, client, captured_emails, db
    ):
        """Indefinite admin lock (locked_until IS NULL) kills the session."""
        _alice(client)
        t0 = login_tokens(client, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL "
            "WHERE username = 'alice'"
        )
        r, _ = refresh_with(client, t0.refresh)
        assert r.status_code == 401
        assert active_session_count(db, "alice") == 0

    def test_refresh_under_timed_lock_still_works(self, client, captured_emails, db):
        """NEW-3 (user decision 2026-11-16): a timed lock from failed logins
        blocks ONLY the password login. Existing sessions keep refreshing."""
        _alice(client)
        t0 = login_tokens(client, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, "
            "locked_until = now() + interval '1 hour' WHERE username = 'alice'"
        )
        r, t1 = refresh_with(client, t0.refresh)
        assert r.status_code == 200, r.text
        assert t1 is not None and t1.refresh != t0.refresh
        assert active_session_count(db, "alice") == 1

    def test_valid_session_survives_redis_flush(
        self, client, captured_emails, flush_redis
    ):
        """Postgres, not Redis, is the source of truth for sessions."""
        _alice(client)
        t0 = login_tokens(client, "alice")
        flush_redis()
        r, t1 = refresh_with(client, t0.refresh)
        assert r.status_code == 200, r.text
        assert t1 is not None and t1.refresh != t0.refresh

    def test_raw_refresh_token_is_not_stored_in_redis(self, client, captured_emails):
        """No raw refresh token may be a Redis key or value (SEC-12 part 1, DB-10)."""
        import os

        import redis as redis_sync

        _alice(client)
        t0 = login_tokens(client, "alice")
        r = redis_sync.Redis(port=int(os.environ["CACHE_PORT"]), decode_responses=True)
        try:
            keys = list(r.scan_iter("*", count=1000))
        finally:
            r.close()
        assert not any(t0.refresh in k for k in keys), keys


# ---------------------------------------------------------------------------
# Session revocation (H2, Known issue 5)
# ---------------------------------------------------------------------------


class TestSessionRevocation:
    def test_access_token_carries_sid_claim_equal_to_session_family(
        self, client, captured_emails, db
    ):
        _alice(client)
        t0 = login_tokens(client, "alice")
        claims = jwt.decode(t0.access, options={"verify_signature": False})
        family = db.scalar("SELECT family_id FROM sessions")
        assert claims.get("sid") == str(family)

    def test_sid_claim_is_stable_across_rotation(self, client, captured_emails):
        _alice(client)
        t0 = login_tokens(client, "alice")
        _, t1 = refresh_with(client, t0.refresh)
        sid0 = jwt.decode(t0.access, options={"verify_signature": False}).get("sid")
        sid1 = jwt.decode(t1.access, options={"verify_signature": False}).get("sid")
        assert sid0 and sid0 == sid1

    def test_password_reset_revokes_every_existing_session(
        self, client, captured_emails, db
    ):
        _alice(client)
        a = login_tokens(client, "alice")
        b = login_tokens(client, "alice")
        assert active_session_count(db, "alice") == 2

        assert _reset_password(client, captured_emails).status_code == 200

        assert refresh_with(client, a.refresh)[0].status_code == 401
        assert refresh_with(client, b.refresh)[0].status_code == 401
        assert active_session_count(db, "alice") == 0

    def test_password_reset_still_allows_login_with_new_password(
        self, client, captured_emails
    ):
        """GUARD: revoking sessions must not break the new credentials."""
        _alice(client)
        login_tokens(client, "alice")
        _reset_password(client, captured_emails, "NewPassword456!")
        r = login(client, username="alice", password="NewPassword456!")
        assert r.status_code == 200

    def test_change_password_revokes_other_sessions_but_keeps_calling_one(
        self, client, captured_emails
    ):
        _alice(client)
        current = login_tokens(client, "alice")
        other = login_tokens(client, "alice")

        r = client.patch(
            "/user/me/password",
            json={"current_password": PASSWORD, "new_password": "NewSecure456!"},
            headers=bearer(current.access),
        )
        assert r.status_code == 200

        assert refresh_with(client, other.refresh)[0].status_code == 401
        assert refresh_with(client, current.refresh)[0].status_code == 200

    def test_change_password_keeps_session_that_was_rotated_before(
        self, client, captured_emails
    ):
        """sid is the family id, so a rotated session is still 'the calling one'."""
        _alice(client)
        first = login_tokens(client, "alice")
        other = login_tokens(client, "alice")
        _, rotated = refresh_with(client, first.refresh)

        r = client.patch(
            "/user/me/password",
            json={"current_password": PASSWORD, "new_password": "NewSecure456!"},
            headers=bearer(rotated.access),
        )
        assert r.status_code == 200
        assert refresh_with(client, other.refresh)[0].status_code == 401
        assert refresh_with(client, rotated.refresh)[0].status_code == 200

    def test_change_password_invalidates_outstanding_reset_tokens(
        self, client, captured_emails
    ):
        _alice(client)
        client.post("/auth/password/forgot", json={"email": "alice@example.com"})
        reset_token = captured_emails.latest_token("alice@example.com", "reset")
        t = login_tokens(client, "alice")

        client.patch(
            "/user/me/password",
            json={"current_password": PASSWORD, "new_password": "NewSecure456!"},
            headers=bearer(t.access),
        )
        r = client.post(
            "/auth/password/reset",
            json={"token": reset_token, "new_password": "AttackerPass789!"},
        )
        assert r.status_code == 400

    def test_logout_all_revokes_every_session_and_clears_cookie(
        self, client, captured_emails, db
    ):
        _alice(client)
        a = login_tokens(client, "alice")
        b = login_tokens(client, "alice")

        r = client.post("/auth/logout-all", headers=bearer(a.access))

        assert r.status_code == 200, r.text
        set_cookie = r.headers.get("set-cookie", "")
        assert "refresh_token=" in set_cookie
        assert "max-age=0" in set_cookie.lower() or "expires=" in set_cookie.lower()
        assert "path=/auth" in set_cookie.lower()
        assert active_session_count(db, "alice") == 0
        assert refresh_with(client, a.refresh)[0].status_code == 401
        assert refresh_with(client, b.refresh)[0].status_code == 401
        assert audit_count(db, "session_revoked", "alice") >= 1

    def test_logout_all_does_not_touch_other_users_sessions(
        self, client, captured_emails, db
    ):
        register(client, username="alice")
        register(client, username="bob")
        a = login_tokens(client, "alice")
        b = login_tokens(client, "bob")

        r = client.post("/auth/logout-all", headers=bearer(a.access))
        assert r.status_code == 200, r.text

        assert active_session_count(db, "bob") == 1
        assert refresh_with(client, b.refresh)[0].status_code == 200

    def test_logout_all_without_token_returns_401(self, client):
        assert client.post("/auth/logout-all").status_code == 401

    def test_logout_all_with_invalid_token_returns_401(self, client):
        r = client.post("/auth/logout-all", headers=bearer("not.a.jwt"))
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Lockout (H4) and admin lock (H5)
# ---------------------------------------------------------------------------


class TestLockout:
    def test_parallel_failed_logins_lock_the_account(self, client, captured_emails, db):
        _alice(client)

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

    def test_sequential_failed_logins_lock_account_at_threshold(
        self, client, captured_emails, db
    ):
        """GUARD: the single-threaded semantics stay as they are."""
        _alice(client)
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            login(client, username="alice", password="WrongPass!1")
        row = db.one("SELECT failed_login_attempts, is_locked FROM users")
        assert row["failed_login_attempts"] == MAXIMUM_LOGIN_ATTEMPTS
        assert row["is_locked"] is True

    def test_admin_lock_without_locked_until_survives_login_attempt(
        self, client, captured_emails, db
    ):
        _alice(client)
        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL "
            "WHERE username = 'alice'"
        )

        assert login(client, username="alice").status_code == 401
        assert login(client, username="alice").status_code == 401

        row = db.one("SELECT is_locked, locked_until FROM users")
        assert row["is_locked"] is True
        assert row["locked_until"] is None

    def test_expired_timed_lock_is_lifted_by_successful_login(
        self, client, captured_emails, db
    ):
        """GUARD: auto-unlock applies only when locked_until is set and past."""
        _alice(client)
        db.execute(
            "UPDATE users SET is_locked = true, failed_login_attempts = 5, "
            "locked_until = now() - interval '1 minute' WHERE username = 'alice'"
        )
        assert login(client, username="alice").status_code == 200
        row = db.one("SELECT is_locked, failed_login_attempts FROM users")
        assert row["is_locked"] is False
        assert row["failed_login_attempts"] == 0

    def test_access_token_of_admin_locked_user_is_rejected(
        self, client, captured_emails, db
    ):
        _alice(client)
        t = login_tokens(client, "alice")
        assert client.get("/user/me", headers=bearer(t.access)).status_code == 200

        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL "
            "WHERE username = 'alice'"
        )

        assert client.get("/user/me", headers=bearer(t.access)).status_code == 401

    def test_access_token_under_timed_lock_is_still_accepted(
        self, client, captured_emails, db
    ):
        """NEW-3: the timed (brute-force) lock does not touch access tokens."""
        _alice(client)
        t = login_tokens(client, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, "
            "locked_until = now() + interval '1 hour' WHERE username = 'alice'"
        )
        assert client.get("/user/me", headers=bearer(t.access)).status_code == 200

    def test_failed_logins_lock_only_the_password_login(
        self, client, captured_emails, db
    ):
        """NEW-3: an attacker who locks the account by guessing must not be able
        to log the real user out. Session, access token and refresh survive;
        only a new password login is refused."""
        _alice(client)
        t = login_tokens(client, "alice")

        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            assert (
                login(client, username="alice", password="WrongPass!1").status_code
                == 401
            )
        client.cookies.clear()
        row = db.one("SELECT is_locked, locked_until FROM users")
        assert row["is_locked"] is True and row["locked_until"] is not None

        assert login(client, username="alice").status_code == 401  # new login: locked
        client.cookies.clear()
        assert client.get("/user/me", headers=bearer(t.access)).status_code == 200
        r, t1 = refresh_with(client, t.refresh)
        assert r.status_code == 200, r.text
        assert client.get("/user/me", headers=bearer(t1.access)).status_code == 200
        assert active_session_count(db, "alice") == 1

    def test_admin_lock_invalidates_sessions_that_were_valid_before(
        self, client, captured_emails, db
    ):
        """Control for the test above: the indefinite lock still ends everything."""
        _alice(client)
        t = login_tokens(client, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL "
            "WHERE username = 'alice'"
        )
        assert client.get("/user/me", headers=bearer(t.access)).status_code == 401
        assert refresh_with(client, t.refresh)[0].status_code == 401
        assert login(client, username="alice").status_code == 401
        client.cookies.clear()

    def test_access_token_of_deactivated_user_is_rejected(
        self, client, captured_emails, db
    ):
        """GUARD."""
        _alice(client)
        t = login_tokens(client, "alice")
        db.execute("UPDATE users SET is_active = false WHERE username = 'alice'")
        assert client.get("/user/me", headers=bearer(t.access)).status_code == 401


# ---------------------------------------------------------------------------
# E-mail tokens are atomically single-use (M3)
# ---------------------------------------------------------------------------


class TestEmailTokensSingleUse:
    def test_parallel_redemption_of_reset_token_succeeds_exactly_once(
        self, client, captured_emails, db
    ):
        _alice(client)
        for round_no in range(ROUNDS):
            client.post("/auth/password/forgot", json={"email": "alice@example.com"})
            token = captured_emails.latest_token("alice@example.com", "reset")

            results = run_parallel(
                8,
                lambda i: client.post(
                    "/auth/password/reset",
                    json={"token": token, "new_password": f"ParallelPass{i}!"},
                ),
            )

            statuses = sorted(r.status_code for r in results)
            assert statuses.count(200) == 1, (round_no, statuses)
            assert statuses.count(400) == 7, (round_no, statuses)
        assert audit_count(db, "password_reset_completed", "alice") == ROUNDS

    def test_parallel_redemption_of_verify_token_succeeds_exactly_once(
        self, client, captured_emails, db
    ):
        for round_no in range(ROUNDS):
            name = f"user{round_no}"
            register(client, username=name)
            token = captured_emails.latest_token(f"{name}@example.com", "verify")

            results = run_parallel(
                8, lambda _i: client.post("/auth/email/verify", json={"token": token})
            )

            statuses = sorted(r.status_code for r in results)
            assert statuses.count(200) == 1, (round_no, statuses)
            assert statuses.count(400) == 7, (round_no, statuses)
        assert audit_count(db, "email_verified") == ROUNDS


# ---------------------------------------------------------------------------
# JWT validation (H16, non-breaking part)
# ---------------------------------------------------------------------------


def _secret() -> str:
    from app.common.config import settings

    return secret_value(settings.JWT_SECRET_KEY)


def _call_me(client, token: str):
    """GET /user/me; a server-side exception counts as a failure of the contract."""
    try:
        return client.get("/user/me", headers=bearer(token))
    except Exception as exc:  # noqa: BLE001 - TestClient re-raises app exceptions
        pytest.fail(f"server raised {type(exc).__name__} instead of answering 401")


class TestJwtValidation:
    def test_forged_token_with_all_claims_is_accepted(
        self, client, captured_emails, db
    ):
        """GUARD (control): proves the negative cases below fail only because of
        the claim they remove."""
        _alice(client)
        token = forge_access_token(user_id_of(db, "alice"), secret=_secret())
        assert _call_me(client, token).status_code == 200

    @pytest.mark.parametrize("claim", ["exp", "sub", "jti", "iat", "nbf"])
    def test_token_missing_required_claim_returns_401(
        self, client, captured_emails, db, claim
    ):
        _alice(client)
        token = forge_access_token(
            user_id_of(db, "alice"), secret=_secret(), drop=(claim,)
        )
        assert _call_me(client, token).status_code == 401

    @pytest.mark.parametrize(
        "sub", ["None", "not-a-uuid", "", "1", "' OR 1=1 --"], ids=repr
    )
    def test_token_with_non_uuid_sub_returns_401(self, client, sub):
        token = forge_access_token("x", secret=_secret(), sub=sub)
        assert _call_me(client, token).status_code == 401

    def test_token_of_wrong_type_returns_401(self, client, captured_emails, db):
        _alice(client)
        token = forge_access_token(
            user_id_of(db, "alice"), secret=_secret(), type="refresh"
        )
        assert _call_me(client, token).status_code == 401

    def test_token_signed_with_other_algorithm_returns_401(
        self, client, captured_emails, db
    ):
        """GUARD: the algorithm list is fixed in code."""
        _alice(client)
        token = forge_access_token(
            user_id_of(db, "alice"), secret=_secret() * 2, algorithm="HS512"
        )
        assert _call_me(client, token).status_code == 401

    def test_unsigned_alg_none_token_returns_401(self, client, captured_emails, db):
        """GUARD."""
        _alice(client)
        claims = jwt.decode(
            forge_access_token(user_id_of(db, "alice"), secret=_secret()),
            options={"verify_signature": False},
        )
        unsigned = jwt.encode(claims, key=None, algorithm="none")
        assert _call_me(client, unsigned).status_code == 401

    def test_expired_token_returns_401(self, client, captured_emails, db):
        """GUARD."""
        from datetime import datetime, timedelta, timezone

        _alice(client)
        token = forge_access_token(
            user_id_of(db, "alice"),
            secret=_secret(),
            exp=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        assert _call_me(client, token).status_code == 401


# ---------------------------------------------------------------------------
# Input bounds (H8, M9)
# ---------------------------------------------------------------------------


class TestInputBounds:
    def test_login_with_2000_char_user_agent_succeeds_and_is_audited(
        self, client, captured_emails, db
    ):
        _alice(client)
        r = login(client, username="alice", headers={"User-Agent": "A" * 2000})
        client.cookies.clear()

        assert r.status_code == 200, r.text
        assert audit_count(db, "login_success", "alice") == 1
        assert db.scalar("SELECT max(length(user_agent)) FROM sessions") <= 512
        assert db.scalar("SELECT max(length(user_agent)) FROM auth_audit_log") <= 512

    @pytest.mark.parametrize("header", ["X-Device-Name", "X-Device-Id"])
    def test_login_with_1000_char_device_header_succeeds(
        self, client, captured_emails, db, header
    ):
        _alice(client)
        r = login(client, username="alice", headers={header: "D" * 1000})
        client.cookies.clear()

        assert r.status_code == 200, r.text
        column = "device_name" if header == "X-Device-Name" else "device_id"
        assert db.scalar(f"SELECT max(length({column})) FROM sessions") <= 255
        assert audit_count(db, "login_success", "alice") == 1

    def test_long_user_agent_on_refresh_succeeds(self, client, captured_emails):
        _alice(client)
        t = login_tokens(client, "alice")
        r = client.post(
            "/auth/refresh",
            headers={
                "Cookie": f"refresh_token={t.refresh}",
                "User-Agent": "A" * 2000,
            },
        )
        assert r.status_code == 200, r.text

    @pytest.mark.parametrize(
        "email",
        ["alice@example.com\n", "alice@example.com\r\n", "alice@@example.com", " "],
        ids=repr,
    )
    def test_register_with_malformed_email_returns_422(self, client, email):
        r = client.post(
            "/auth/register",
            json={"username": "alice", "email": email, "password": PASSWORD},
        )
        assert r.status_code == 422

    def test_register_with_trailing_newline_creates_no_user(self, client, db):
        client.post(
            "/auth/register",
            json={
                "username": "alice",
                "email": "alice@example.com\n",
                "password": PASSWORD,
            },
        )
        assert db.scalar("SELECT count(*) FROM users") == 0

    @pytest.mark.parametrize("email", ["not-an-email", "a@b.com\n"], ids=repr)
    def test_forgot_password_with_malformed_email_returns_422(self, client, email):
        r = client.post("/auth/password/forgot", json={"email": email})
        assert r.status_code == 422

    def test_reset_mail_is_sent_to_the_stored_address(self, client, captured_emails):
        register(client, username="alice", email="alice@example.com")
        captured_emails.clear()

        r = client.post("/auth/password/forgot", json={"email": "ALICE@Example.COM"})

        assert r.status_code == 200
        resets = captured_emails.of_kind("reset")
        assert len(resets) == 1
        assert resets[0].to == "alice@example.com"

    def test_forgot_for_unknown_address_sends_no_mail(self, client, captured_emails):
        """GUARD: no mail and still 200 (no enumeration)."""
        r = client.post("/auth/password/forgot", json={"email": "ghost@example.com"})
        assert r.status_code == 200
        assert captured_emails == []

    def test_registration_sends_exactly_one_verification_mail(
        self, client, captured_emails
    ):
        """GUARD."""
        register(client, username="alice")
        assert len(captured_emails) == 1
        assert captured_emails[0].kind == "verify"
        assert captured_emails[0].to == "alice@example.com"


# ---------------------------------------------------------------------------
# Logging hygiene (H9)
# ---------------------------------------------------------------------------

PII_USER = "zebra_user_91"
PII_MAIL = "zebra.mail91@example.com"


def _log_text(caplog) -> str:
    return "\n".join(f"{r.name} {r.msg} {r.getMessage()}" for r in caplog.records)


class TestLoggingHygiene:
    def test_auth_flows_do_not_log_username_or_email(
        self, client, captured_emails, caplog
    ):
        caplog.set_level(logging.DEBUG)

        register(client, username=PII_USER, email=PII_MAIL)
        login(client, username=PII_USER, password="WrongPass!1")  # failed login
        login(client, username="ghost_" + PII_USER)  # unknown user
        t = login_tokens(client, PII_USER)
        client.get("/user/me", headers=bearer(t.access))
        client.post("/auth/password/forgot", json={"email": PII_MAIL})
        refresh_with(client, t.refresh)
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):  # also exercises the lock log
            login(client, username=PII_USER, password="WrongPass!1")
        client.cookies.clear()

        text = _log_text(caplog)
        assert caplog.records, "no log records captured; test is not observing logs"
        assert PII_USER not in text
        assert PII_MAIL not in text

    def test_tokens_are_not_logged(self, client, captured_emails, caplog):
        caplog.set_level(logging.DEBUG)
        _alice(client)
        t = login_tokens(client, "alice")
        client.get("/user/me", headers=bearer(t.access))
        refresh_with(client, t.refresh)
        text = _log_text(caplog)
        assert t.refresh[:8] not in text
        assert t.access[:20] not in text

    @pytest.mark.parametrize("outcome", ["sent", "failed"])
    @pytest.mark.parametrize("task_name", ["verification", "password_reset"])
    def test_email_tasks_do_not_log_recipient_or_username(
        self, monkeypatch, caplog, task_name, outcome
    ):
        import asyncio

        from app.utils import email_tasks

        async def _send(**_kwargs):
            if outcome == "failed":
                raise RuntimeError("smtp down")

        monkeypatch.setattr(email_tasks, "send_templated_email", _send)
        task = (
            email_tasks.send_verification_email_task
            if task_name == "verification"
            else email_tasks.send_password_reset_email_task
        )
        caplog.set_level(logging.DEBUG)

        try:
            asyncio.run(task.original_func(PII_MAIL, PII_USER, "tok-123"))
        except RuntimeError:
            assert outcome == "failed"

        text = _log_text(caplog)
        assert caplog.records
        assert PII_MAIL not in text
        assert PII_USER not in text
        assert "tok-123" not in text

    def test_access_log_has_no_query_string_or_referer(self, client, caplog):
        access = logging.getLogger("access")
        access.addHandler(caplog.handler)
        caplog.set_level(logging.DEBUG, logger="access")
        try:
            client.get(
                "/?token=SUPERSECRETQUERY",
                headers={"Referer": "https://example.org/?reset=SECRETREFERER"},
            )
        finally:
            access.removeHandler(caplog.handler)
        text = _log_text(caplog)
        assert "http.access" in text, "access log not observed"
        assert "SUPERSECRETQUERY" not in text
        assert "SECRETREFERER" not in text

    def test_database_url_password_is_not_logged(self, monkeypatch, caplog):
        """GUARD (fixed in step A, K3): engine creation and init_db logs."""
        import asyncio

        import app.common.database as database
        from app.common.config import settings

        password = "S3cr3tPw-Zq91xx"
        monkeypatch.setattr(
            settings,
            "DATABASE_URL",
            f"postgresql+psycopg://dbuser:{password}@localhost:1/db",
        )
        caplog.set_level(logging.DEBUG)

        engine = database._build_engine()  # logs db.engine_creating
        asyncio.run(engine.dispose())
        try:
            asyncio.run(database.init_db())  # uses the real engine, logs the URL
        except RuntimeError:
            pass

        text = _log_text(caplog)
        assert caplog.records
        assert password not in text
