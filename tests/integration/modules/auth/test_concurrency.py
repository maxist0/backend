"""Concurrency regression tests for the auth flows.

* `TestRevocationVersusRotation`: a revoke `UPDATE` that waits on the row lock
  of an in-flight refresh rotation must also revoke the successor session
  inserted by that refresh (READ COMMITTED).
* `TestLoginBurst`: the lock check runs before the Argon2 slot wait and the
  success path re-checks it, so N parallel guesses do not cause N evaluations.
* `TestConcurrentRegistration`: a racing duplicate registration yields one 201
  and no 500, and the `IntegrityError` text (`DETAIL: Key (email)=(..)`) never
  reaches the log.
* `TestAccountDisabledRefresh`: `revoke_family` commits only if it found rows,
  so a retry after the denied refresh must not look like token reuse.

Tests marked "GUARD" are expected green already.

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

import logging
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.modules.auth.service import MAXIMUM_LOGIN_ATTEMPTS
from tests.helpers import (
    PASSWORD,
    active_session_count,
    audit_count,
    bearer,
    login,
    login_tokens,
    register,
    run_parallel,
)

ROUNDS = 3


def _alice(client):
    assert register(client, username="alice").status_code == 201


def _log_text(caplog) -> str:
    """All log text including formatted tracebacks (they carry exception messages)."""
    parts: list[str] = []
    for r in caplog.records:
        parts.append(f"{r.name} {r.msg} {r.getMessage()}")
        if r.exc_text:
            parts.append(r.exc_text)
        if r.exc_info:
            parts.append("".join(traceback.format_exception(*r.exc_info)))
    return "\n".join(parts)


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
def rotation_gate(monkeypatch):
    import asyncio

    import app.modules.auth.service as service

    original = service.create_session_tokens
    gate = _RotationGate()

    async def gated(*args, **kwargs):
        family_id = kwargs.get("family_id", args[3] if len(args) > 3 else None)
        if family_id is not None and gate.armed:  # refresh path, not login
            gate.armed = False
            gate.reached.set()
            released = await asyncio.get_running_loop().run_in_executor(
                None, gate.release.wait, 30
            )
            assert released, "rotation gate was never released"
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "create_session_tokens", gated)
    yield gate
    gate.release.set()  # never leave a request hanging if the test aborted


def _refresh_raw(client, refresh_token: str):
    """POST /auth/refresh; returns (response, new refresh token or None).

    Reads the new token from the response itself, not from the shared cookie
    jar: during the race another response could modify the jar.
    """
    r = client.post(
        "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={refresh_token}"}
    )
    return r, r.cookies.get("refresh_token") if r.status_code == 200 else None


def _wait_until_a_backend_is_blocked(db, future, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if future.done():
            return False
        if db.scalar(_BLOCKED_BACKENDS_SQL):
            return True
        time.sleep(0.02)
    return False


def _race_revocation_against_rotation(client, db, gate, refresh_token, revoke):
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
    def test_logout_all_during_rotation_revokes_the_successor_too(
        self, client, captured_emails, db, rotation_gate
    ):
        _alice(client)
        t0 = login_tokens(client, "alice")

        refresh_r, successor, revoke_r = _race_revocation_against_rotation(
            client,
            db,
            rotation_gate,
            t0.refresh,
            lambda: client.post("/api/v1/auth/logout-all", headers=bearer(t0.access)),
        )

        assert refresh_r.status_code == 200 and successor, refresh_r.text
        assert revoke_r.status_code == 204, revoke_r.text
        assert active_session_count(db, "alice") == 0
        assert _refresh_raw(client, successor)[0].status_code == 401

    def test_password_reset_during_rotation_revokes_the_successor_too(
        self, client, captured_emails, db, rotation_gate
    ):
        _alice(client)
        t0 = login_tokens(client, "alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        reset_token = captured_emails.latest_token("alice@example.com", "reset")

        refresh_r, successor, revoke_r = _race_revocation_against_rotation(
            client,
            db,
            rotation_gate,
            t0.refresh,
            lambda: client.post(
                "/api/v1/auth/password/reset",
                json={"token": reset_token, "new_password": "NewPassword456!"},
            ),
        )

        assert refresh_r.status_code == 200 and successor, refresh_r.text
        assert revoke_r.status_code == 204, revoke_r.text
        assert active_session_count(db, "alice") == 0
        assert _refresh_raw(client, successor)[0].status_code == 401

    def test_change_password_during_rotation_revokes_the_successor_of_other_session(
        self, client, captured_emails, db, rotation_gate
    ):
        _alice(client)
        rotating = login_tokens(client, "alice")
        caller = login_tokens(client, "alice")

        refresh_r, successor, revoke_r = _race_revocation_against_rotation(
            client,
            db,
            rotation_gate,
            rotating.refresh,
            lambda: client.put(
                "/api/v1/users/me/password",
                json={"current_password": PASSWORD, "new_password": "NewSecure456!"},
                headers=bearer(caller.access),
            ),
        )

        assert refresh_r.status_code == 200 and successor, refresh_r.text
        assert revoke_r.status_code == 204, revoke_r.text
        assert _refresh_raw(client, successor)[0].status_code == 401
        # only the calling session survives
        assert active_session_count(db, "alice") == 1

    def test_replay_during_rotation_revokes_the_successor(
        self, client, captured_emails, db, rotation_gate
    ):
        """GUARD: the reuse path (revoke_family) re-reads after the rotation commit.
        Must stay green once the revocation primitives get their user lock."""
        _alice(client)
        t0 = login_tokens(client, "alice")

        refresh_r, successor, replay_r = _race_revocation_against_rotation(
            client,
            db,
            rotation_gate,
            t0.refresh,
            lambda: _refresh_raw(client, t0.refresh)[0],
        )

        assert refresh_r.status_code == 200 and successor, refresh_r.text
        assert replay_r.status_code == 401
        assert active_session_count(db, "alice") == 0
        assert _refresh_raw(client, successor)[0].status_code == 401


@pytest.fixture
def verify_spy(monkeypatch, db):
    """Make a login burst deterministic and count the password evaluations.

    * Every request is held after the initial lock check until the whole burst
      has passed it (``burst`` requests have returned from the user lookup), so
      all of them see an unlocked account, as in a real burst that arrives
      faster than one Argon2 verify takes. The wait has a timeout and then
      proceeds, so a renamed lookup cannot hang the test.
    * The evaluation of the correct password is additionally held until the
      account is locked: a request that queued for an Argon2 slot while other
      guesses locked the account.
    The real Argon2 verify still runs behind the spy.
    """
    import asyncio

    import app.modules.auth.service as service
    from app.modules.users import crud

    original_verify = service.verify_password
    original_lookup = crud.get_user_by_username

    class _State:
        wrong = correct = arrived = burst = 0
        all_arrived = threading.Event()

        def expect_burst(self, size: int) -> None:
            """Reset the counters right before a burst of `size` logins
            (registration also calls the lookup, so reset after the setup)."""
            self.wrong = self.correct = self.arrived = 0
            self.burst = size
            self.all_arrived.clear()

    state = _State()

    async def lookup(db_session, username):
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
        return await original_verify(password, password_hash)

    monkeypatch.setattr(crud, "get_user_by_username", lookup)
    monkeypatch.setattr(service, "verify_password", spy)
    return state


class TestLoginBurst:
    def test_burst_at_the_threshold_evaluates_few_guesses_and_never_logs_in(
        self, client, captured_emails, db, verify_spy
    ):
        """30 parallel logins at attempts=4, one with the correct password, all
        past the initial lock check before the first verify finishes.

        Once the lock is set no login may succeed, and the number of evaluated
        wrong guesses must stay near the threshold instead of the burst size.
        Allowed: the one guess that locks the account plus the evaluations that
        were already in flight (Argon2 slots) and the ones started in the
        turnover while that lock was committed. Needs the lock to be re-checked
        after the Argon2 slot wait (or the attempt to be reserved atomically
        before the verify) plus the conditional success UPDATE.
        """
        from app.core.config import get_settings

        settings = get_settings()

        _alice(client)
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
                f"{verify_spy.wrong} wrong guesses were evaluated "
                f"(allowed {allowed_evaluations})",
            )
            statuses = sorted(r.status_code for r in results)
            assert statuses == [401] * burst, (round_no, statuses)
            row = db.one(
                "SELECT failed_login_attempts, is_locked FROM users "
                "WHERE username = 'alice'"
            )
            assert row["is_locked"] is True, (round_no, dict(row))
            assert row["failed_login_attempts"] >= MAXIMUM_LOGIN_ATTEMPTS

    def test_correct_password_after_the_lock_never_creates_a_session(
        self, client, captured_emails, db, verify_spy
    ):
        """The correct password is evaluated while the account is already locked
        (held by the spy): the success path must re-check and refuse."""
        _alice(client)
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


class TestConcurrentRegistration:
    @pytest.mark.parametrize("duplicate", ["email", "username"])
    def test_parallel_duplicate_registration_yields_one_201_and_no_500(
        self, client, captured_emails, db, caplog, duplicate
    ):
        caplog.set_level(logging.DEBUG)
        racers = 8
        for round_no in range(ROUNDS):
            shared_email = f"race{round_no}.mail@example.com"
            shared_name = f"racer{round_no}x"

            def _register(i: int, _e=shared_email, _n=shared_name):
                if duplicate == "email":
                    return register(client, username=f"u{round_no}n{i}", email=_e)
                return register(
                    client, username=_n, email=f"u{round_no}n{i}@example.com"
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
                        "SELECT count(*) FROM users WHERE username = :n", n=shared_name
                    )
                    == 1
                )

        text = _log_text(caplog)
        assert caplog.records, "no log records captured; test is not observing logs"
        assert "race0.mail@example.com" not in text
        assert "racer0x" not in text
        assert "DETAIL" not in text, "raw database error text reached the logs"

    def test_email_change_confirmation_racing_a_registration_never_fails_with_500(
        self, client, captured_emails, db, caplog
    ):
        """Confirming an email change while the address is being registered:
        exactly one account gets it; the loser gets 409 (pre-check or unique
        constraint), never a 500, and the address never reaches the logs."""
        caplog.set_level(logging.DEBUG)
        _alice(client)
        token = login_tokens(client, "alice").access
        shared = "contested.mail@example.com"
        r = client.put(
            "/api/v1/users/me/email",
            json={"current_password": PASSWORD, "new_email": shared},
            headers=bearer(token),
        )
        assert r.status_code == 202, r.text
        change_token = captured_emails.latest_token(shared, "change_email")

        def _act(i: int):
            if i == 0:
                return register(client, username="bob", email=shared)
            return client.post(
                "/api/v1/auth/email/verify", json={"token": change_token}
            )

        results = run_parallel(2, _act)
        client.cookies.clear()

        statuses = sorted(r.status_code for r in results)
        assert statuses in ([201, 409], [204, 409]), statuses
        assert db.scalar("SELECT count(*) FROM users WHERE email = :e", e=shared) == 1
        assert shared not in _log_text(caplog)


_DISABLE = {
    "deactivated": "is_active = false",
    "admin_locked": "is_locked = true, locked_until = NULL",
}


class TestAccountDisabledRefresh:
    @pytest.mark.parametrize("disable_sql", _DISABLE.values(), ids=_DISABLE.keys())
    def test_retry_after_denied_refresh_is_not_reported_as_token_reuse(
        self, client, captured_emails, db, caplog, disable_sql
    ):
        """Only session of a disabled account: the denied refresh ends it, and a
        retry with the same cookie is a plain 401, not a theft signal."""
        caplog.set_level(logging.DEBUG)
        _alice(client)
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
        assert "auth.token_reuse_detected" not in _log_text(caplog)

    @pytest.mark.parametrize("disable_sql", _DISABLE.values(), ids=_DISABLE.keys())
    def test_denied_refresh_persists_the_revocation_without_the_audit_write(
        self, client, captured_emails, db, monkeypatch, disable_sql
    ):
        """revoke_family must commit on its own: today the rotation is only
        persisted as a side effect of the audit insert that follows it.
        `_audit` is stubbed to isolate exactly that commit path."""
        _alice(client)
        t0 = login_tokens(client, "alice")
        db.execute(f"UPDATE users SET {disable_sql} WHERE username = 'alice'")

        async def _no_audit(*_args, **_kwargs) -> None:
            return None

        monkeypatch.setattr("app.modules.auth.service._audit", _no_audit)

        r = _refresh_raw(client, t0.refresh)[0]
        client.cookies.clear()

        assert r.status_code == 401
        assert db.scalar("SELECT count(*) FROM sessions WHERE NOT is_revoked") == 0
