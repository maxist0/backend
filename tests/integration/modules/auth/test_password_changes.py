"""A password change ends every other session, also against parallel requests.

* `TestResetEndsEverySession`: the reset token is redeemed in the same
  transaction as the new hash, the token invalidation and the session
  revocation, so nothing the old password does meanwhile survives it, and a
  failure before that changes nothing.
* `TestLoginRacingAPasswordChange`: a login that verified the old password
  while a reset or change committed gets no session (`password_changed_at`).
* `TestChangeRacingAReset`: a password change whose confirmation saw an older
  password never overwrites a reset committed meanwhile.
* `TestAccessTokensAfterAPasswordChange`: access tokens issued before the
  last password change or reset are refused.
* `TestEmailTokensAfterAPasswordChange`: an email token created before the
  last password change or reset is refused at redemption and never mailed,
  and an email change confirmed with the old password creates no token.
* `TestRehash`: the login's rehash never overwrites a newer password, and a
  failed rehash does not fail the login.
* `TestLockRace`: a lock committed between a check and the write that
  follows it is never erased or ignored.
* `TestLocksAreReleasedBeforeArgon2`: no user or email-token row lock is held
  while Argon2 runs (`FOR NO KEY UPDATE NOWAIT` from a second connection).

Pausing works like the rotation gate in `test_concurrency.py`: a patched
coroutine waits on a `threading.Event` in an executor, so the app's event
loop keeps serving the other requests.
"""

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Never

import pytest
import time_machine
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core import hash_password, verify_password
from app.modules.auth import service
from app.modules.auth.tasks import send_token_email_task
from app.modules.users import User
from app.modules.users.dependencies import get_optional_user
from tests.helpers import (
    NEW_PASSWORD,
    PASSWORD,
    CapturedEmails,
    Db,
    Tokens,
    active_session_count,
    app_of,
    audit_count,
    bearer,
    call_in_app,
    change_email,
    change_password,
    log_events,
    login,
    login_tokens,
    refresh_with,
    register_ok,
    request_reset,
    reset_password,
    run_task,
    user_id_of,
    verify_email,
)

pytestmark = pytest.mark.usefixtures("captured_emails")


class _Gate:
    """Pauses the first call of one patched coroutine function.

    `background` runs the request that hits the gate in a second thread, so
    the test thread can send other requests meanwhile.
    """

    def __init__(self, pool: ThreadPoolExecutor) -> None:
        self.reached = threading.Event()
        self.release = threading.Event()
        self.armed = True
        self.background = pool.submit

    def wrap[**P, R](
        self, original: Callable[P, Awaitable[R]]
    ) -> Callable[P, Awaitable[R]]:
        async def gated(*args: P.args, **kwargs: P.kwargs) -> R:
            if self.armed:
                self.armed = False
                self.reached.set()
                released = await asyncio.get_running_loop().run_in_executor(
                    None, self.release.wait, 30
                )
                assert released, "gate was never released"
            return await original(*args, **kwargs)

        return gated


@pytest.fixture
def gate() -> Iterator[_Gate]:
    with ThreadPoolExecutor(max_workers=1) as pool:
        g = _Gate(pool)
        yield g
        g.release.set()  # never leave a request hanging if the test aborted


def _outdated_hash(password: str = PASSWORD) -> str:
    """A hash of `password` made with other Argon2 parameters than configured."""
    from app.core.config import get_settings

    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.ARGON2_TIME_COST + 1,
        memory_cost=settings.ARGON2_MEMORY_KIB,
        parallelism=settings.ARGON2_PARALLELISM,
    ).hash(password)


def _session_rows(db: Db) -> int:
    return int(db.scalar("SELECT count(*) FROM sessions"))


async def _fail(*_args: object, **_kwargs: object) -> Never:
    raise RuntimeError("hashing failed")


class TestResetEndsEverySession:
    def test_a_hash_failure_leaves_the_token_open_and_the_sessions_intact(
        self,
        client: TestClient,
        db: Db,
        captured_emails: CapturedEmails,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        register_ok(client, username="alice")
        login_tokens(client, "alice")
        token = request_reset(client, captured_emails)
        monkeypatch.setattr(service, "hash_password", _fail)

        assert reset_password(client, token).status_code == 500

        assert active_session_count(db, "alice") == 1
        assert (
            db.scalar(
                "SELECT used_at FROM email_tokens WHERE purpose = 'reset_password'"
            )
            is None
        )
        monkeypatch.undo()
        assert reset_password(client, token).status_code == 204

    def test_a_login_while_the_new_password_is_hashed_does_not_survive_it(
        self,
        client: TestClient,
        db: Db,
        captured_emails: CapturedEmails,
        monkeypatch: pytest.MonkeyPatch,
        gate: _Gate,
    ) -> None:
        register_ok(client, username="alice")
        token = request_reset(client, captured_emails)
        monkeypatch.setattr(service, "hash_password", gate.wrap(hash_password))

        pending = gate.background(reset_password, client, token)
        assert gate.reached.wait(30)
        old_login = login(client, username="alice")
        client.cookies.clear()
        gate.release.set()

        assert pending.result(timeout=60).status_code == 204
        assert old_login.status_code == 200
        assert active_session_count(db, "alice") == 0
        assert login(client, username="alice").status_code == 401
        assert login(client, username="alice", password=NEW_PASSWORD).status_code == 200


class TestLoginRacingAPasswordChange:
    @pytest.mark.parametrize("change", ["reset", "change"])
    def test_a_login_that_verified_the_old_password_gets_no_session(
        self,
        client: TestClient,
        db: Db,
        captured_emails: CapturedEmails,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        gate: _Gate,
        change: str,
    ) -> None:
        register_ok(client, username="alice")
        access = login_tokens(client, "alice").access
        token = request_reset(client, captured_emails)
        monkeypatch.setattr(service, "verify_password", gate.wrap(verify_password))
        caplog.set_level(logging.INFO)

        pending = gate.background(login, client, username="alice")
        assert gate.reached.wait(30)
        if change == "reset":
            assert reset_password(client, token).status_code == 204
        else:
            assert change_password(client, access, new=NEW_PASSWORD).status_code == 204
        sessions_before = _session_rows(db)
        gate.release.set()
        r = pending.result(timeout=60)
        client.cookies.clear()

        assert r.status_code == 401
        assert r.json()["code"] == "invalid_credentials"
        assert _session_rows(db) == sessions_before
        assert db.scalar("SELECT failed_login_attempts FROM users") == 0
        assert log_events(caplog, "auth.login_credentials_changed")
        assert login(client, username="alice", password=NEW_PASSWORD).status_code == 200

    def test_password_changed_at_is_set_by_changes_but_not_by_a_rehash(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        assert db.scalar("SELECT password_changed_at FROM users") is None
        db.execute(
            "UPDATE password_credentials SET password_hash = :h", h=_outdated_hash()
        )
        assert login(client, username="alice").status_code == 200
        assert db.scalar("SELECT password_changed_at FROM users") is None

        assert (
            reset_password(client, request_reset(client, captured_emails)).status_code
            == 204
        )
        after_reset: datetime = db.scalar("SELECT password_changed_at FROM users")
        assert after_reset is not None

        access = login_tokens(client, "alice", NEW_PASSWORD).access
        assert (
            change_password(client, access, NEW_PASSWORD, PASSWORD).status_code == 204
        )
        assert db.scalar("SELECT password_changed_at FROM users") > after_reset


class TestChangeRacingAReset:
    def test_a_change_confirmed_before_a_reset_does_not_overwrite_it(
        self,
        client: TestClient,
        db: Db,
        captured_emails: CapturedEmails,
        monkeypatch: pytest.MonkeyPatch,
        gate: _Gate,
    ) -> None:
        """The holder of the old password and an access token loses the race."""
        register_ok(client, username="alice")
        access = login_tokens(client, "alice").access
        token = request_reset(client, captured_emails)
        monkeypatch.setattr(service, "hash_password", gate.wrap(hash_password))

        pending = gate.background(change_password, client, access, new="Intruder123!")
        assert gate.reached.wait(30)
        assert reset_password(client, token).status_code == 204
        gate.release.set()
        r = pending.result(timeout=60)

        assert r.status_code == 400
        assert r.json()["code"] == "wrong_password"
        assert db.scalar("SELECT failed_login_attempts FROM users") == 0
        assert audit_count(db, "password_changed", "alice") == 0
        assert login(client, username="alice", password=NEW_PASSWORD).status_code == 200
        assert (
            login(client, username="alice", password="Intruder123!").status_code == 401
        )


def _login_earlier(client: TestClient, seconds: int = 10) -> Tokens:
    """Log in with the application clock `seconds` in the past (an older `iat`)."""
    with time_machine.travel(datetime.now(UTC) - timedelta(seconds=seconds)):
        return login_tokens(client, "alice")


def _me(client: TestClient, access: str) -> int:
    return client.get("/api/v1/users/me", headers=bearer(access)).status_code


class TestAccessTokensAfterAPasswordChange:
    def test_a_token_from_before_a_reset_is_refused(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        register_ok(client, username="alice")
        old = _login_earlier(client)
        assert _me(client, old.access) == 200
        caplog.set_level(logging.INFO)

        assert (
            reset_password(client, request_reset(client, captured_emails)).status_code
            == 204
        )
        r = client.get("/api/v1/users/me", headers=bearer(old.access))

        assert r.status_code == 401
        assert r.json()["code"] == "unauthorized"
        assert r.headers["www-authenticate"] == "Bearer"
        [event] = log_events(caplog, "auth.access_token_outdated")
        assert set(event) >= {"user_id"}
        new = login_tokens(client, "alice", NEW_PASSWORD)
        assert _me(client, new.access) == 200

    def test_after_an_own_change_the_caller_refreshes(self, client: TestClient) -> None:
        """The caller's session survives its own change; its access token not."""
        register_ok(client, username="alice")
        old = _login_earlier(client)

        assert change_password(client, old.access, new=NEW_PASSWORD).status_code == 204

        assert _me(client, old.access) == 401
        r, fresh = refresh_with(client, old.refresh)
        assert r.status_code == 200
        assert fresh is not None
        assert _me(client, fresh.access) == 200

    def test_a_token_issued_in_the_second_of_the_change_is_kept(
        self, client: TestClient
    ) -> None:
        """`iat` has whole seconds: the same second counts as after the change."""
        register_ok(client, username="alice")
        second = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=30)
        with time_machine.travel(second, tick=False):
            tokens = login_tokens(client, "alice")
        with time_machine.travel(second + timedelta(milliseconds=500), tick=False):
            assert (
                change_password(client, tokens.access, new=NEW_PASSWORD).status_code
                == 204
            )

        assert _me(client, tokens.access) == 200

    def test_the_optional_user_ignores_an_outdated_token(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        old = _login_earlier(client)
        assert (
            reset_password(client, request_reset(client, captured_emails)).status_code
            == 204
        )

        async def _resolve() -> User | None:
            async with app_of(client).state.session_factory() as session:
                return await get_optional_user(session, old.access)

        assert call_in_app(client, _resolve) is None


_NEW_EMAIL = "new@example.com"


def _open_change_email_tokens(db: Db) -> int:
    return int(
        db.scalar(
            "SELECT count(*) FROM email_tokens WHERE purpose = 'change_email' "
            "AND used_at IS NULL AND invalidated_at IS NULL"
        )
    )


def _password_changed_relative_to_token(db: Db, purpose: str, offset: str) -> None:
    """Set `password_changed_at` to the token's `created_at` plus `offset`."""
    db.execute(
        "UPDATE users SET password_changed_at = "
        "(SELECT created_at FROM email_tokens WHERE purpose = :p) "
        "+ CAST(:o AS interval)",
        p=purpose,
        o=offset,
    )


class TestEmailTokensAfterAPasswordChange:
    def test_an_email_change_confirmed_before_a_reset_creates_no_token(
        self,
        client: TestClient,
        db: Db,
        captured_emails: CapturedEmails,
        monkeypatch: pytest.MonkeyPatch,
        gate: _Gate,
    ) -> None:
        register_ok(client, username="alice")
        access = login_tokens(client, "alice").access
        token = request_reset(client, captured_emails)
        monkeypatch.setattr(service, "verify_password", gate.wrap(verify_password))

        pending = gate.background(change_email, client, access, _NEW_EMAIL)
        assert gate.reached.wait(30)
        assert reset_password(client, token).status_code == 204
        gate.release.set()
        r = pending.result(timeout=60)

        assert r.status_code == 400
        assert r.json()["code"] == "wrong_password"
        assert _open_change_email_tokens(db) == 0
        assert captured_emails.to(_NEW_EMAIL) == []
        assert db.scalar("SELECT failed_login_attempts FROM users") == 0
        assert db.scalar("SELECT email FROM users") == "alice@example.com"

    @pytest.mark.parametrize(
        ("offset", "status"),
        [("1 second", 400), ("0 seconds", 400), ("-1 millisecond", 204)],
        ids=["changed_after", "same_instant", "changed_before"],
    )
    def test_redemption_refuses_a_token_created_before_the_change(
        self,
        client: TestClient,
        db: Db,
        captured_emails: CapturedEmails,
        offset: str,
        status: int,
    ) -> None:
        """A reset that missed the token (it was not committed yet) still wins."""
        register_ok(client, username="alice")
        access = login_tokens(client, "alice").access
        assert change_email(client, access, _NEW_EMAIL).status_code == 202
        mail_token = captured_emails.latest_token(_NEW_EMAIL, "change_email")
        _password_changed_relative_to_token(db, "change_email", offset)

        r = verify_email(client, mail_token)

        assert r.status_code == status
        if status == 400:
            assert r.json()["code"] == "invalid_token"
            assert db.scalar("SELECT email FROM users") == "alice@example.com"
            assert _open_change_email_tokens(db) == 1
        else:
            assert db.scalar("SELECT email FROM users") == _NEW_EMAIL

    def test_the_worker_mails_nothing_for_a_token_created_before_the_change(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        access = login_tokens(client, "alice").access
        assert change_email(client, access, _NEW_EMAIL).status_code == 202
        sent = len(captured_emails.to(_NEW_EMAIL))
        stored = db.scalar(
            "SELECT token_hash FROM email_tokens WHERE purpose = 'change_email'"
        )
        _password_changed_relative_to_token(db, "change_email", "1 second")

        token_id = str(
            db.scalar("SELECT id FROM email_tokens WHERE purpose = 'change_email'")
        )
        run_task(client, send_token_email_task, token_id)

        assert len(captured_emails.to(_NEW_EMAIL)) == sent
        assert (
            db.scalar(
                "SELECT token_hash FROM email_tokens WHERE purpose = 'change_email'"
            )
            == stored
        )

    def test_a_reset_token_created_before_the_change_is_refused(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        token = request_reset(client, captured_emails)
        before = db.scalar("SELECT password_hash FROM password_credentials")
        _password_changed_relative_to_token(db, "reset_password", "1 second")

        r = reset_password(client, token)

        assert r.status_code == 400
        assert r.json()["code"] == "invalid_token"
        assert db.scalar("SELECT password_hash FROM password_credentials") == before

    def test_a_token_requested_after_a_reset_works(
        self, client: TestClient, db: Db, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        assert (
            reset_password(client, request_reset(client, captured_emails)).status_code
            == 204
        )
        access = login_tokens(client, "alice", NEW_PASSWORD).access

        assert change_email(client, access, _NEW_EMAIL, NEW_PASSWORD).status_code == 202
        mail_token = captured_emails.latest_token(_NEW_EMAIL, "change_email")
        assert verify_email(client, mail_token).status_code == 204
        assert db.scalar("SELECT email FROM users") == _NEW_EMAIL


class TestRehash:
    def test_a_failed_rehash_keeps_the_login_and_the_old_hash(
        self,
        client: TestClient,
        db: Db,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        register_ok(client, username="alice")
        outdated = _outdated_hash()
        db.execute("UPDATE password_credentials SET password_hash = :h", h=outdated)
        monkeypatch.setattr("app.modules.auth.crud.update_password_hash", _fail)
        caplog.set_level(logging.INFO)

        r = login(client, username="alice")
        client.cookies.clear()

        assert r.status_code == 200, r.text
        assert active_session_count(db, "alice") == 1
        assert db.scalar("SELECT password_hash FROM password_credentials") == outdated
        [event] = log_events(caplog, "auth.password_rehash_failed")
        assert event["error_type"] == "RuntimeError"

    def test_a_password_change_during_the_rehash_is_kept(
        self,
        client: TestClient,
        db: Db,
        monkeypatch: pytest.MonkeyPatch,
        gate: _Gate,
    ) -> None:
        register_ok(client, username="alice")
        access = login_tokens(client, "alice").access
        db.execute(
            "UPDATE password_credentials SET password_hash = :h", h=_outdated_hash()
        )
        monkeypatch.setattr(service, "hash_password", gate.wrap(hash_password))

        pending = gate.background(login, client, username="alice")
        assert gate.reached.wait(30)
        assert change_password(client, access, new=NEW_PASSWORD).status_code == 204
        gate.release.set()
        assert pending.result(timeout=60).status_code == 200
        client.cookies.clear()

        assert login(client, username="alice", password=NEW_PASSWORD).status_code == 200
        assert login(client, username="alice").status_code == 401


class TestLockRace:
    def test_a_lock_committed_after_the_expiry_check_is_not_erased(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET is_locked = true, failed_login_attempts = 5, "
            "locked_until = now() - interval '1 minute'"
        )
        original = User.is_lock_active
        calls: list[None] = []

        def _relocked_meanwhile(self: User, now: datetime | None = None) -> bool:
            if calls:
                return original(self, now)
            calls.append(None)
            # A parallel guess locks the account again right after this check.
            db.execute("UPDATE users SET locked_until = now() + interval '15 minutes'")
            return False

        monkeypatch.setattr(User, "is_lock_active", _relocked_meanwhile)

        r = login(client, username="alice")

        assert r.status_code == 401
        row = db.one("SELECT is_locked, locked_until > now() AS future FROM users")
        assert (row["is_locked"], row["future"]) == (True, True)
        assert audit_count(db, "account_unlocked", "alice") == 0

    def test_a_confirmation_refused_by_a_parallel_lock_is_audited(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(client, username="alice")
        access = login_tokens(client, "alice").access
        original = verify_password

        async def _locked_while_verifying(password: str, password_hash: str) -> bool:
            valid = await original(password, password_hash)
            db.execute(
                "UPDATE users SET is_locked = true, "
                "locked_until = now() + interval '15 minutes'"
            )
            return valid

        monkeypatch.setattr(service, "verify_password", _locked_while_verifying)

        r = change_password(client, access, new=NEW_PASSWORD)

        assert r.status_code == 400
        assert r.json()["code"] == "wrong_password"
        assert (
            db.scalar(
                "SELECT count(*) FROM auth_audit_log "
                "WHERE detail = 'Password confirmation, account locked'"
            )
            == 1
        )


class TestLocksAreReleasedBeforeArgon2:
    """Each spy tries the row locks from a second connection, without waiting."""

    @staticmethod
    def _spy[**P, R](
        db: Db,
        statements: list[str],
        user_id: Callable[[], str],
        original: Callable[P, Awaitable[R]],
        found: list[str],
    ) -> Callable[P, Awaitable[R]]:
        async def spy(*args: P.args, **kwargs: P.kwargs) -> R:
            for sql in statements:
                try:
                    db.execute(sql, id=user_id())
                except OperationalError as exc:
                    found.append(f"{sql}: {type(exc.orig).__name__}")
            found.append("checked")
            return await original(*args, **kwargs)

        return spy

    _USER_ROW = "SELECT 1 FROM users WHERE id = :id FOR NO KEY UPDATE NOWAIT"
    _TOKEN_ROWS = (
        "SELECT 1 FROM email_tokens WHERE user_id = :id FOR NO KEY UPDATE NOWAIT"
    )

    def test_expired_lock_clear_before_the_password_check(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET is_locked = true, failed_login_attempts = 5, "
            "locked_until = now() - interval '1 minute'"
        )
        found: list[str] = []
        monkeypatch.setattr(
            service,
            "verify_password",
            self._spy(
                db,
                [self._USER_ROW],
                lambda: user_id_of(db, "alice"),
                verify_password,
                found,
            ),
        )

        assert login(client, username="alice").status_code == 200
        assert found == ["checked"]

    def test_login_rehash(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(client, username="alice")
        db.execute(
            "UPDATE password_credentials SET password_hash = :h", h=_outdated_hash()
        )
        found: list[str] = []
        monkeypatch.setattr(
            service,
            "hash_password",
            self._spy(
                db,
                [self._USER_ROW],
                lambda: user_id_of(db, "alice"),
                hash_password,
                found,
            ),
        )

        assert login(client, username="alice").status_code == 200
        assert found == ["checked"]

    def test_change_password_after_the_confirmation(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(client, username="alice")
        access = login_tokens(client, "alice").access
        found: list[str] = []
        monkeypatch.setattr(
            service,
            "hash_password",
            self._spy(
                db,
                [self._USER_ROW, self._TOKEN_ROWS],
                lambda: user_id_of(db, "alice"),
                hash_password,
                found,
            ),
        )

        assert change_password(client, access, new=NEW_PASSWORD).status_code == 204
        assert found == ["checked"]

    def test_reset_password_before_hashing(
        self,
        client: TestClient,
        db: Db,
        captured_emails: CapturedEmails,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        register_ok(client, username="alice")
        token = request_reset(client, captured_emails)
        found: list[str] = []
        monkeypatch.setattr(
            service,
            "hash_password",
            self._spy(
                db,
                [self._USER_ROW, self._TOKEN_ROWS],
                lambda: user_id_of(db, "alice"),
                hash_password,
                found,
            ),
        )

        assert reset_password(client, token).status_code == 204
        assert found == ["checked"]
