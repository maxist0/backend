"""Parallel requests on the auth flows.

* `TestRevocationVersusRotation`: a revoke `UPDATE` that waits on the row lock
  of an in-flight refresh rotation must also revoke the successor session
  inserted by that refresh (READ COMMITTED).
* `TestLoginBurst`: the lock check runs before the Argon2 slot wait and the
  success path re-checks it, so N parallel guesses do not cause N evaluations.
* `TestConcurrentRegistration`: a racing duplicate registration yields one 201
  and no 500, and the `IntegrityError` text (`DETAIL: Key (email)=(..)`) never
  reaches the log.
* `TestAccountDisabledRefresh`: the denied refresh commits its revocation as
  `account_disabled`, so a retry with the same token must not look like reuse.
* `TestEmailTokensSingleUse`: parallel redemptions of one email token succeed
  exactly once.

Design of the rotation-versus-revocation tests (primitive-agnostic,
deterministic, no sleeps in the code under test):

1. A real `POST /auth/refresh` is paused inside its transaction: the test
   replaces `service.create_session_tokens` (the successor insert and commit)
   by a wrapper that signals "rotation done, successor not yet committed" and
   then waits.
2. A real revocation request (logout-all, password reset, change password,
   token replay) is started and the test waits until Postgres reports a backend
   in `wait_event_type = 'Lock'` (`pg_stat_activity`). Whatever primitive the
   implementation picks (row lock, `SELECT ... FOR UPDATE` on users, advisory
   lock), the revocation has to queue behind the open rotation transaction.
3. The rotation is released and commits its successor. Only then the
   assertions run: the successor must be revoked as well.
"""

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor

import pytest
from fastapi import Request
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import verify_password
from app.core.config import get_settings
from app.modules.auth import service
from app.modules.users import User, crud
from tests.helpers import (
    MAXIMUM_LOGIN_ATTEMPTS,
    PASSWORD,
    CapturedEmails,
    Db,
    active_session_count,
    audit_count,
    bearer,
    change_email,
    change_password,
    log_text,
    login,
    login_tokens,
    register,
    register_ok,
    reset_password,
    run_parallel,
    verify_email,
)

ROUNDS = 3
REDEMPTION_ROUNDS = 4


_BLOCKED_BACKENDS_SQL = (
    "SELECT count(*) FROM pg_stat_activity "
    "WHERE wait_event_type = 'Lock' AND datname = current_database()"
)


class _RotationGate:
    def __init__(self) -> None:
        self.reached = threading.Event()  # rotation done, successor not committed
        self.release = threading.Event()  # let the refresh finish
        self.armed = True  # only the first refresh is paused


@pytest.fixture
def rotation_gate(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RotationGate]:
    original = service.create_session_tokens
    gate = _RotationGate()

    async def gated(
        db: AsyncSession,
        user: User,
        request: Request | None = None,
        family: service.SessionFamily | None = None,
    ) -> service.SessionTokens:
        if family is not None and gate.armed:  # refresh path, not login
            gate.armed = False
            gate.reached.set()
            released = await asyncio.get_running_loop().run_in_executor(
                None, gate.release.wait, 30
            )
            assert released, "rotation gate was never released"
        return await original(db, user, request, family)

    monkeypatch.setattr(service, "create_session_tokens", gated)
    yield gate
    gate.release.set()  # never leave a request hanging if the test aborted


def _refresh_raw(client: TestClient, refresh_token: str) -> tuple[Response, str | None]:
    """POST /auth/refresh; returns (response, new refresh token or None).

    Reads the new token from the response itself, not from the shared cookie
    jar: during the race another response could modify the jar.
    """
    r = client.post(
        "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={refresh_token}"}
    )
    return r, r.cookies.get("refresh_token") if r.status_code == 200 else None


def _wait_until_a_backend_is_blocked(
    db: Db, future: Future[Response], timeout: float = 20.0
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if future.done():
            return False
        if db.scalar(_BLOCKED_BACKENDS_SQL):
            return True
        time.sleep(0.02)
    return False


def _race_revocation_against_rotation(
    client: TestClient,
    db: Db,
    gate: _RotationGate,
    refresh_token: str,
    revoke: Callable[[], Response],
) -> tuple[Response, str | None, Response]:
    """Pause a refresh mid-rotation, start `revoke`, then let the refresh commit."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        refresh_future = pool.submit(_refresh_raw, client, refresh_token)
        assert gate.reached.wait(30), "refresh never reached the rotation gate"
        revoke_future = pool.submit(revoke)
        try:
            blocked = _wait_until_a_backend_is_blocked(db, revoke_future)
        finally:
            gate.release.set()
        refresh_response, successor = refresh_future.result(60)
        revoke_response = revoke_future.result(60)
    assert blocked, (
        "the revocation did not queue behind the open rotation transaction, "
        "so this run proves nothing"
    )
    client.cookies.clear()
    return refresh_response, successor, revoke_response


class TestRevocationVersusRotation:
    @pytest.mark.usefixtures("captured_emails")
    def test_logout_all_during_rotation_revokes_the_successor_too(
        self, client: TestClient, db: Db, rotation_gate: _RotationGate
    ) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")

        refresh_r, successor, revoke_r = _race_revocation_against_rotation(
            client,
            db,
            rotation_gate,
            t0.refresh,
            lambda: client.post("/api/v1/auth/logout-all", headers=bearer(t0.access)),
        )

        assert refresh_r.status_code == 200, refresh_r.text
        assert successor
        assert revoke_r.status_code == 204, revoke_r.text
        assert active_session_count(db, "alice") == 0
        assert _refresh_raw(client, successor)[0].status_code == 401

    def test_password_reset_during_rotation_revokes_the_successor_too(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        rotation_gate: _RotationGate,
    ) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        reset_token = captured_emails.latest_token("alice@example.com", "reset")

        refresh_r, successor, revoke_r = _race_revocation_against_rotation(
            client,
            db,
            rotation_gate,
            t0.refresh,
            lambda: reset_password(client, reset_token, "NewPassword456!"),
        )

        assert refresh_r.status_code == 200, refresh_r.text
        assert successor
        assert revoke_r.status_code == 204, revoke_r.text
        assert active_session_count(db, "alice") == 0
        assert _refresh_raw(client, successor)[0].status_code == 401

    @pytest.mark.usefixtures("captured_emails")
    def test_change_password_during_rotation_revokes_the_successor_of_other_session(
        self, client: TestClient, db: Db, rotation_gate: _RotationGate
    ) -> None:
        register_ok(client, username="alice")
        rotating = login_tokens(client, "alice")
        caller = login_tokens(client, "alice")

        refresh_r, successor, revoke_r = _race_revocation_against_rotation(
            client,
            db,
            rotation_gate,
            rotating.refresh,
            lambda: change_password(client, caller.access),
        )

        assert refresh_r.status_code == 200, refresh_r.text
        assert successor
        assert revoke_r.status_code == 204, revoke_r.text
        assert _refresh_raw(client, successor)[0].status_code == 401
        # only the calling session survives
        assert active_session_count(db, "alice") == 1

    @pytest.mark.usefixtures("captured_emails")
    def test_replay_during_rotation_revokes_the_successor(
        self, client: TestClient, db: Db, rotation_gate: _RotationGate
    ) -> None:
        """The reuse path (`revoke_family`) re-reads after the rotation commit."""
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")

        refresh_r, successor, replay_r = _race_revocation_against_rotation(
            client,
            db,
            rotation_gate,
            t0.refresh,
            lambda: _refresh_raw(client, t0.refresh)[0],
        )

        assert refresh_r.status_code == 200, refresh_r.text
        assert successor
        assert replay_r.status_code == 401
        assert active_session_count(db, "alice") == 0
        assert _refresh_raw(client, successor)[0].status_code == 401


class _LoginBurstSpy:
    """Counters of the password evaluations during one login burst."""

    def __init__(self) -> None:
        self.wrong = self.correct = self.arrived = self.burst = 0
        self.all_arrived = threading.Event()

    def expect_burst(self, size: int) -> None:
        """Reset the counters right before a burst of `size` logins.

        Registration also calls the lookup, so reset after the setup.
        """
        self.wrong = self.correct = self.arrived = 0
        self.burst = size
        self.all_arrived.clear()


@pytest.fixture
def verify_spy(monkeypatch: pytest.MonkeyPatch, db: Db) -> _LoginBurstSpy:
    """Make a login burst deterministic and count the password evaluations.

    * Every request is held after the initial lock check until the whole burst
      has passed it (`burst` requests have returned from the user lookup), so
      all of them see an unlocked account, as in a real burst that arrives
      faster than one Argon2 verify takes. The wait has a timeout and then
      proceeds, so a renamed lookup cannot hang the test.
    * The evaluation of the correct password is additionally held until the
      account is locked: a request that queued for an Argon2 slot while other
      guesses locked the account.
    The real Argon2 verify still runs behind the spy.
    """
    original_lookup = crud.get_user_by_username
    state = _LoginBurstSpy()

    async def lookup(db_session: AsyncSession, username: str) -> User | None:
        user = await original_lookup(db_session, username)
        state.arrived += 1  # no await between this and the caller's lock check
        if state.arrived >= state.burst:
            state.all_arrived.set()
        return user

    def _wait_until_locked() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if db.scalar("SELECT is_locked FROM users WHERE username = 'alice'"):
                return
            time.sleep(0.02)
        raise AssertionError("the account was never locked by the other guesses")

    async def spy(password: str, password_hash: str) -> bool:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, state.all_arrived.wait, 10)
        if password == PASSWORD:
            state.correct += 1
            await loop.run_in_executor(None, _wait_until_locked)
        else:
            state.wrong += 1
        return await verify_password(password, password_hash)

    monkeypatch.setattr(crud, "get_user_by_username", lookup)
    monkeypatch.setattr(service, "verify_password", spy)
    return state


# Many requests from one client: this tests the mechanism behind the rate
# limits, which must also hold while they are off (fail-open).
@pytest.mark.usefixtures("without_rate_limits")
class TestLoginBurst:
    @pytest.mark.usefixtures("captured_emails")
    def test_burst_at_the_threshold_evaluates_few_guesses_and_never_logs_in(
        self, client: TestClient, db: Db, verify_spy: _LoginBurstSpy
    ) -> None:
        """30 parallel logins at the last attempt evaluate few guesses, log none in.

        One login has the correct password; all pass the initial lock check
        before the first verify finishes.
        Once the lock is set no login may succeed, and the number of evaluated
        wrong guesses must stay near the threshold instead of the burst size.
        Allowed: the one guess that locks the account plus the evaluations that
        were already in flight (Argon2 slots) and the ones started in the
        turnover while that lock was committed. Needs the lock to be re-checked
        after the Argon2 slot wait (or the attempt to be reserved atomically
        before the verify) plus the conditional success UPDATE.
        """
        settings = get_settings()

        register_ok(client, username="alice")
        burst, correct_index = 30, 0
        allowed_evaluations = 1 + 2 * settings.PASSWORD_HASH_CONCURRENCY

        for round_no in range(ROUNDS):
            db.execute(
                "UPDATE users SET failed_login_attempts = :n, is_locked = false, "
                "locked_until = NULL WHERE username = 'alice'",
                n=MAXIMUM_LOGIN_ATTEMPTS - 1,
            )
            verify_spy.expect_burst(burst)

            results = run_parallel(
                burst,
                lambda i: login(
                    client,
                    username="alice",
                    password=PASSWORD if i == correct_index else "WrongPass!1",
                ),
            )
            client.cookies.clear()

            assert verify_spy.wrong <= allowed_evaluations, (
                round_no,
                (
                    f"{verify_spy.wrong} wrong guesses were evaluated "
                    f"(allowed {allowed_evaluations})"
                ),
            )
            statuses = sorted(r.status_code for r in results)
            assert statuses == [401] * burst, (round_no, statuses)
            row = db.one(
                "SELECT failed_login_attempts, is_locked FROM users "
                "WHERE username = 'alice'"
            )
            assert row["is_locked"] is True, (round_no, dict(row))
            assert row["failed_login_attempts"] >= MAXIMUM_LOGIN_ATTEMPTS

    @pytest.mark.usefixtures("captured_emails")
    def test_correct_password_after_the_lock_never_creates_a_session(
        self, client: TestClient, db: Db, verify_spy: _LoginBurstSpy
    ) -> None:
        """The success path re-checks the lock before it creates a session.

        The spy holds the correct password's evaluation until the other
        guesses have locked the account.
        """
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET failed_login_attempts = :n WHERE username = 'alice'",
            n=MAXIMUM_LOGIN_ATTEMPTS - 1,
        )
        verify_spy.expect_burst(10)

        results = run_parallel(
            10,
            lambda i: login(
                client,
                username="alice",
                password=PASSWORD if i == 0 else "WrongPass!1",
            ),
        )
        client.cookies.clear()

        assert sorted(r.status_code for r in results) == [401] * 10
        assert active_session_count(db, "alice") == 0
        assert audit_count(db, "login_success", "alice") == 0


# Many requests from one client: this tests the mechanism behind the rate
# limits, which must also hold while they are off (fail-open).
@pytest.mark.usefixtures("without_rate_limits")
class TestConcurrentRegistration:
    @pytest.mark.parametrize("duplicate", ["email", "username"])
    def test_parallel_duplicate_registration_yields_one_201_and_no_500(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        duplicate: str,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        racers = 8
        for round_no in range(ROUNDS):
            shared_email = f"race{round_no}.mail@example.com"
            shared_name = f"racer{round_no}x"

            def _register(
                i: int,
                prefix: str = f"u{round_no}n",
                email: str = shared_email,
                name: str = shared_name,
            ) -> Response:
                if duplicate == "email":
                    return register(client, username=f"{prefix}{i}", email=email)
                # Half of the racers differ only in case: one user either way.
                return register(
                    client,
                    username=name.upper() if i % 2 else name,
                    email=f"{prefix}{i}@example.com",
                )

            results = run_parallel(racers, _register)

            statuses = sorted(r.status_code for r in results)
            assert statuses == [201] + [409] * (racers - 1), (round_no, statuses)
            loser = next(r for r in results if r.status_code == 409).json()
            expected = (
                "Email already registered"
                if duplicate == "email"
                else "Username already taken"
            )
            assert loser["code"] == "user_exists"
            assert loser["detail"] == expected
            if duplicate == "email":
                assert (
                    db.scalar(
                        "SELECT count(*) FROM users WHERE email = :e", e=shared_email
                    )
                    == 1
                )
                assert len(captured_emails.to(shared_email)) == 1
            else:
                assert (
                    db.scalar(
                        "SELECT count(*) FROM users WHERE lower(username) = :n",
                        n=shared_name,
                    )
                    == 1
                )

        text = log_text(caplog)
        assert caplog.records, "no log records captured; test is not observing logs"
        assert "race0.mail@example.com" not in text
        assert "racer0x" not in text
        assert "RACER0X" not in text
        assert "DETAIL" not in text, "raw database error text reached the logs"

    @pytest.mark.usefixtures("captured_emails")
    def test_unique_index_decides_a_username_the_pre_check_missed(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case-insensitive index answers 409 like the pre-check would."""
        register_ok(client, username="alice")

        async def _missed(_db: AsyncSession, _username: str) -> User | None:
            return None

        monkeypatch.setattr(crud, "get_user_by_username", _missed)

        r = register(client, username="ALICE", email="other@example.com")

        assert r.status_code == 409
        assert r.json()["code"] == "user_exists"
        assert r.json()["detail"] == "Username already taken"
        assert db.scalar("SELECT count(*) FROM users") == 1

    def test_email_change_confirmation_racing_a_registration_never_fails_with_500(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """An email change confirmed while the address is being registered.

        Exactly one account gets it; the loser gets 409 (pre-check or unique
        constraint), never a 500, and the address never reaches the logs.
        """
        caplog.set_level(logging.DEBUG)
        register_ok(client, username="alice")
        token = login_tokens(client, "alice").access
        shared = "contested.mail@example.com"
        r = change_email(client, token, shared)
        assert r.status_code == 202, r.text
        change_token = captured_emails.latest_token(shared, "change_email")

        def _act(i: int) -> Response:
            if i == 0:
                return register(client, username="bob", email=shared)
            return verify_email(client, change_token)

        results = run_parallel(2, _act)
        client.cookies.clear()

        statuses = sorted(r.status_code for r in results)
        assert statuses in ([201, 409], [204, 409]), statuses
        assert db.scalar("SELECT count(*) FROM users WHERE email = :e", e=shared) == 1
        assert shared not in log_text(caplog)


_DISABLE = {
    "deactivated": "is_active = false",
    "admin_locked": "is_locked = true, locked_until = NULL",
}


class TestAccountDisabledRefresh:
    @pytest.mark.usefixtures("captured_emails")
    @pytest.mark.parametrize("disable_sql", _DISABLE.values(), ids=_DISABLE.keys())
    def test_retry_after_denied_refresh_is_not_reported_as_token_reuse(
        self,
        client: TestClient,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        disable_sql: str,
    ) -> None:
        """A retry after a denied refresh is a plain 401, not a theft signal.

        The only session of a disabled account ends with the denied refresh.
        """
        caplog.set_level(logging.DEBUG)
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        db.execute(f"UPDATE users SET {disable_sql} WHERE username = 'alice'")

        first = _refresh_raw(client, t0.refresh)[0]
        assert first.status_code == 401
        assert db.scalar("SELECT count(*) FROM sessions WHERE NOT is_revoked") == 0

        retry = _refresh_raw(client, t0.refresh)[0]
        client.cookies.clear()
        assert retry.status_code == 401

        assert (
            db.scalar(
                "SELECT count(*) FROM auth_audit_log "
                "WHERE lower(event_type::text) = 'session_revoked' "
                "AND lower(detail) LIKE '%reuse%'"
            )
            == 0
        )
        assert "auth.token_reuse_detected" not in log_text(caplog)

    @pytest.mark.usefixtures("captured_emails")
    @pytest.mark.parametrize("disable_sql", _DISABLE.values(), ids=_DISABLE.keys())
    def test_denied_refresh_persists_the_revocation_without_the_audit_write(
        self,
        client: TestClient,
        db: Db,
        monkeypatch: pytest.MonkeyPatch,
        disable_sql: str,
    ) -> None:
        """The denied refresh commits the revocation, audit row or not.

        `_audit` is stubbed, so no audit insert is part of the commit.
        """
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        db.execute(f"UPDATE users SET {disable_sql} WHERE username = 'alice'")

        async def _no_audit(*_args: object, **_kwargs: object) -> None:
            return None

        monkeypatch.setattr("app.modules.auth.service._audit", _no_audit)

        r = _refresh_raw(client, t0.refresh)[0]
        client.cookies.clear()

        assert r.status_code == 401
        assert db.scalar("SELECT count(*) FROM sessions WHERE NOT is_revoked") == 0

    @pytest.mark.usefixtures("captured_emails")
    @pytest.mark.parametrize("disable_sql", _DISABLE.values(), ids=_DISABLE.keys())
    def test_denied_refresh_commits_the_revocation_with_its_audit_row(
        self, client: TestClient, db: Db, disable_sql: str
    ) -> None:
        register_ok(client, username="alice")
        t0 = login_tokens(client, "alice")
        db.execute(f"UPDATE users SET {disable_sql} WHERE username = 'alice'")

        r = _refresh_raw(client, t0.refresh)[0]
        client.cookies.clear()

        assert r.status_code == 401
        assert db.scalar("SELECT count(*) FROM sessions WHERE NOT is_revoked") == 0
        assert (
            db.scalar(
                "SELECT detail FROM auth_audit_log WHERE event_type = 'session_revoked'"
            )
            == "Refresh denied: account inactive or locked, revoked 1 session(s)"
        )


# Many requests from one client: this tests the mechanism behind the rate
# limits, which must also hold while they are off (fail-open).
@pytest.mark.usefixtures("without_rate_limits")
class TestEmailTokensSingleUse:
    def test_parallel_redemption_of_reset_token_succeeds_exactly_once(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        for round_no in range(REDEMPTION_ROUNDS):
            client.post(
                "/api/v1/auth/password/forgot", json={"email": "alice@example.com"}
            )
            token = captured_emails.latest_token("alice@example.com", "reset")

            def _redeem(i: int, token: str = token) -> Response:
                return reset_password(client, token, f"ParallelPass{i}!")

            results = run_parallel(8, _redeem)

            statuses = sorted(r.status_code for r in results)
            assert statuses.count(204) == 1, (round_no, statuses)
            assert statuses.count(400) == 7, (round_no, statuses)
        assert audit_count(db, "password_reset_completed", "alice") == REDEMPTION_ROUNDS

    def test_parallel_redemption_of_verify_token_succeeds_exactly_once(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        for round_no in range(REDEMPTION_ROUNDS):
            name = f"user{round_no}"
            register(client, username=name)
            token = captured_emails.latest_token(f"{name}@example.com", "verify")

            def _verify(_i: int, token: str = token) -> Response:
                return verify_email(client, token)

            results = run_parallel(8, _verify)

            statuses = sorted(r.status_code for r in results)
            assert statuses.count(204) == 1, (round_no, statuses)
            assert statuses.count(400) == 7, (round_no, statuses)
        assert audit_count(db, "email_verified") == REDEMPTION_ROUNDS

    def test_parallel_redemption_of_email_change_token_succeeds_exactly_once(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        for round_no in range(REDEMPTION_ROUNDS):
            new_email = f"alice{round_no}@example.org"
            r = change_email(client, t.access, new_email)
            assert r.status_code == 202, r.text
            token = captured_emails.latest_token(new_email, "change_email")

            def _verify(_i: int, token: str = token) -> Response:
                return verify_email(client, token)

            results = run_parallel(8, _verify)

            statuses = sorted(r.status_code for r in results)
            assert statuses.count(204) == 1, (round_no, statuses)
            assert statuses.count(400) == 7, (round_no, statuses)
            assert db.scalar("SELECT email FROM users") == new_email
        assert audit_count(db, "email_changed", "alice") == REDEMPTION_ROUNDS
