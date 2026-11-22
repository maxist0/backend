"""Security notices: the account-locked mail and the email-change notices.

The email-change notices go to the old address. The tasks run for real:
in-process for the `client` fixture, so a request returns after its notice
task ran; through the Redis stream for the
transport tests, where no worker consumes the entries.
"""

import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Never

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import BrokerMessage

from app.core.security import hash_token
from app.core.tasks import TASK_STREAM
from app.modules.auth import crud, service
from app.modules.auth.tasks import (
    lock_end_text,
    send_account_locked_notice_task,
    send_email_change_completed_notice_task,
    send_email_change_requested_notice_task,
)
from tests.helpers import (
    MAXIMUM_LOGIN_ATTEMPTS,
    PII_EMAIL,
    PII_USERNAME,
    CapturedEmails,
    CapturingMailer,
    Db,
    app_of,
    assert_nothing_personal_in_redis,
    audit_count,
    call_in_app,
    change_email,
    change_password,
    log_events,
    login,
    login_tokens,
    redis_from_env,
    register_ok,
    run_task,
    user_id_of,
    verify_email,
)

OLD_MAIL = "alice@example.com"
NEW_MAIL = "new.alice@example.com"
NOTICE_SENT = "account_locked_notice_sent"


def _not_sent_reasons(caplog: pytest.LogCaptureFixture, purpose: str) -> list[str]:
    return [
        e["reason"]
        for e in log_events(caplog, "email.not_sent")
        if e.get("purpose") == purpose
    ]


def _register_verified(client: TestClient, db: Db, username: str = "alice") -> None:
    """Register; mark the address verified, as notices need (no token mail)."""
    register_ok(client, username=username)
    db.execute(
        "UPDATE users SET is_email_verified = true WHERE username = :u", u=username
    )


def _lock_by_login(client: TestClient, username: str = "alice") -> list[Response]:
    """Send wrong passwords until the timed lock is set; return the answers."""
    return [
        login(client, username=username, password="wrong-password")
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS)
    ]


def _lift_timed_lock(db: Db, username: str = "alice") -> None:
    db.execute(
        "UPDATE users SET is_locked = false, locked_until = NULL, "
        "failed_login_attempts = 0 WHERE username = :u",
        u=username,
    )


def _admin(
    client: TestClient,
    action: Callable[..., Awaitable[bool]],
    user_id: str,
) -> None:
    """Run an admin service action on the app's loop, as the admin panel does."""

    async def _run() -> None:
        session: AsyncSession
        async with app_of(client).state.session_factory() as session:
            assert await action(session, uuid.UUID(user_id), actor="admin")

    call_in_app(client, _run)


def _change_token_id(db: Db, username: str = "alice") -> str:
    return str(
        db.scalar(
            "SELECT t.id FROM email_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE u.username = :u AND t.purpose = 'change_email' "
            "ORDER BY t.created_at DESC LIMIT 1",
            u=username,
        )
    )


class TestAccountLockedNotice:
    def test_login_lock_mails_the_user_once_with_the_lock_end(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        _register_verified(client, db)
        captured_emails.clear()

        _lock_by_login(client)

        [mail] = captured_emails.of_kind("account_locked")
        assert mail.to == OLD_MAIL
        assert mail.username == "alice"
        assert mail.token is None
        assert mail.link == "http://localhost:5173"
        locked_until = db.scalar(
            "SELECT locked_until FROM users WHERE username = 'alice'"
        )
        assert lock_end_text(locked_until, datetime.now(UTC)) in mail.html
        assert " UTC" in mail.html
        assert audit_count(db, NOTICE_SENT, "alice") == 1

    def test_locking_answer_equals_any_other_wrong_password_answer(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        _register_verified(client, db)

        *before, locking = _lock_by_login(client)

        assert len(captured_emails.of_kind("account_locked")) == 1
        for response in (*before, locking):
            assert response.status_code == 401
        body = locking.json()
        reference = before[-1].json()
        assert body["code"] == "invalid_credentials"
        assert {k: v for k, v in body.items() if k != "request_id"} == {
            k: v for k, v in reference.items() if k != "request_id"
        }
        assert set(locking.headers) == set(before[-1].headers)
        assert (
            locking.headers["www-authenticate"]
            == before[-1].headers["www-authenticate"]
        )

    def test_password_confirmation_lock_mails_the_user_once(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        captured_emails.clear()

        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            r = change_password(client, tokens.access, "wrong", "N3w-Password!")
            assert r.status_code == 400
            assert r.json()["code"] == "wrong_password"

        [mail] = captured_emails.of_kind("account_locked")
        assert mail.to == OLD_MAIL
        assert audit_count(db, NOTICE_SENT, "alice") == 1

    def test_admin_lock_sends_no_mail(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        _register_verified(client, db)
        captured_emails.clear()

        _admin(client, service.admin_lock_user, user_id_of(db, "alice"))

        assert captured_emails == []
        assert audit_count(db, "account_locked", "alice") == 1
        assert audit_count(db, NOTICE_SENT, "alice") == 0

    def test_lock_replaced_by_an_admin_lock_is_not_mailed(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The admin lock has no end; a notice would state a wrong one."""
        _register_verified(client, db)
        captured_emails.clear()
        user_id = user_id_of(db, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, locked_until = NULL WHERE id = :u",
            u=user_id,
        )
        caplog.set_level(logging.INFO)

        run_task(client, send_account_locked_notice_task, user_id)

        assert captured_emails == []
        assert _not_sent_reasons(caplog, "account_locked") == ["admin_locked"]

    def test_throttle_holds_across_two_locks_and_ends_after_a_day(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _register_verified(client, db)
        captured_emails.clear()
        caplog.set_level(logging.INFO)

        _lock_by_login(client)
        _lift_timed_lock(db)
        _lock_by_login(client)

        assert len(captured_emails.of_kind("account_locked")) == 1
        assert audit_count(db, "account_locked", "alice") == 2
        assert audit_count(db, NOTICE_SENT, "alice") == 1
        assert _not_sent_reasons(caplog, "account_locked") == ["recently_notified"]

        db.execute(
            "UPDATE auth_audit_log "
            "SET created_at = now() - interval '24 hours 1 minute' "
            "WHERE event_type = 'account_locked_notice_sent'"
        )
        _lift_timed_lock(db)
        _lock_by_login(client)

        assert len(captured_emails.of_kind("account_locked")) == 2

    def test_recent_admin_lock_does_not_throttle_the_notice(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        _register_verified(client, db)
        user_id = user_id_of(db, "alice")
        _admin(client, service.admin_lock_user, user_id)
        _admin(client, service.admin_unlock_user, user_id)
        captured_emails.clear()

        _lock_by_login(client)

        assert len(captured_emails.of_kind("account_locked")) == 1

    def test_unsent_notice_does_not_throttle_the_next_one(
        self, client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only a sent notice is recorded; a lock without mail does not count."""
        _register_verified(client, db)
        _lock_by_login(client)  # mail is not configured: nothing is sent
        assert audit_count(db, NOTICE_SENT, "alice") == 0
        emails = CapturedEmails()
        mailer = CapturingMailer(emails)
        monkeypatch.setattr("app.core.mail.get_mailer", lambda: mailer)
        monkeypatch.setattr("app.modules.auth.tasks.get_mailer", lambda: mailer)
        _lift_timed_lock(db)

        _lock_by_login(client)

        assert [m.kind for m in emails] == ["account_locked"]
        assert audit_count(db, NOTICE_SENT, "alice") == 1

    @pytest.mark.parametrize(
        ("state", "reason"),
        [
            # The timed lock expired before the worker ran.
            ("locked_until = now() - interval '1 second'", "not_locked"),
            # An admin unlocked the account meanwhile.
            ("is_locked = false, locked_until = NULL", "not_locked"),
            ("is_active = false", "account_inactive"),
        ],
    )
    def test_nothing_is_sent_when_the_lock_is_gone_or_the_account_inactive(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        state: str,
        reason: str,
    ) -> None:
        _register_verified(client, db)
        user_id = user_id_of(db, "alice")
        db.execute(
            "UPDATE users SET is_locked = true, "
            "locked_until = now() + interval '10 minutes' WHERE id = :u",
            u=user_id,
        )
        db.execute(f"UPDATE users SET {state} WHERE id = :u", u=user_id)
        captured_emails.clear()
        caplog.set_level(logging.INFO)

        run_task(client, send_account_locked_notice_task, user_id)

        assert captured_emails == []
        assert _not_sent_reasons(caplog, "account_locked") == [reason]
        assert audit_count(db, NOTICE_SENT, "alice") == 0

    def test_unverified_address_gets_no_lock_notice(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        register_ok(client, username="alice")
        captured_emails.clear()
        caplog.set_level(logging.INFO)

        _lock_by_login(client)

        assert captured_emails.of_kind("account_locked") == []
        assert _not_sent_reasons(caplog, "account_locked") == ["unverified_address"]
        assert audit_count(db, "account_locked", "alice") == 1
        assert audit_count(db, NOTICE_SENT, "alice") == 0

    def test_enqueue_failure_keeps_the_answer(
        self, client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        _register_verified(client, db)
        caplog.set_level(logging.INFO)

        async def _kick(_message: BrokerMessage) -> Never:
            raise ConnectionError("broker down")

        app_of(client).state.broker.kick = _kick
        *_, locking = _lock_by_login(client)

        assert locking.status_code == 401
        assert locking.json()["code"] == "invalid_credentials"
        [event] = log_events(caplog, "auth.email_enqueue_failed")
        assert event["purpose"] == "account_locked"
        assert event["user_id"] == user_id_of(db, "alice")


class TestEmailChangeNotices:
    def test_request_notifies_the_current_address(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        captured_emails.clear()

        assert change_email(client, tokens.access, NEW_MAIL).status_code == 202

        [notice] = captured_emails.of_kind("email_change_requested")
        assert (notice.to, notice.username, notice.token) == (OLD_MAIL, "alice", None)
        assert notice.link == "http://localhost:5173"
        [confirmation] = captured_emails.of_kind("change_email")
        assert confirmation.to == NEW_MAIL

    def test_confirmation_stores_and_notifies_the_old_address(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        change_email(client, tokens.access, NEW_MAIL)
        token_id = _change_token_id(db)
        assert (
            db.scalar(
                "SELECT previous_email FROM email_tokens WHERE id = :t", t=token_id
            )
            is None
        )
        token = captured_emails.latest_token(NEW_MAIL, "change_email")

        r = verify_email(client, token)

        assert r.status_code == 204
        assert db.scalar("SELECT email FROM users") == NEW_MAIL
        assert (
            db.scalar(
                "SELECT previous_email FROM email_tokens WHERE id = :t", t=token_id
            )
            == OLD_MAIL
        )
        [notice] = captured_emails.of_kind("email_change_completed")
        assert (notice.to, notice.username, notice.token) == (OLD_MAIL, "alice", None)

    def test_verification_tokens_get_no_previous_email(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        token = captured_emails.latest_token(OLD_MAIL, "verify")

        verify_email(client, token)

        assert db.scalar("SELECT count(*) FROM email_tokens") == 1
        assert db.scalar("SELECT previous_email FROM email_tokens") is None
        assert captured_emails.of_kind("email_change_completed") == []

    def test_unverified_current_address_gets_no_request_notice(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        register_ok(client, username="alice")
        tokens = login_tokens(client, "alice")
        captured_emails.clear()
        caplog.set_level(logging.INFO)

        assert change_email(client, tokens.access, NEW_MAIL).status_code == 202

        assert [m.kind for m in captured_emails] == ["change_email"]
        assert _not_sent_reasons(caplog, "email_change_requested") == [
            "unverified_address"
        ]

    def test_unverified_old_address_is_neither_stored_nor_notified(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        register_ok(client, username="alice")
        tokens = login_tokens(client, "alice")
        change_email(client, tokens.access, NEW_MAIL)
        token = captured_emails.latest_token(NEW_MAIL, "change_email")
        captured_emails.clear()
        caplog.set_level(logging.INFO)

        r = verify_email(client, token)

        assert r.status_code == 204
        assert db.scalar("SELECT email FROM users") == NEW_MAIL
        assert db.scalar("SELECT previous_email FROM email_tokens") is None
        assert captured_emails == []
        assert _not_sent_reasons(caplog, "email_change_completed") == [
            "unverified_address"
        ]
        # A late requested notice must not fall back to the new address.
        caplog.clear()
        run_task(client, send_email_change_requested_notice_task, _change_token_id(db))
        assert captured_emails == []
        assert _not_sent_reasons(caplog, "email_change_requested") == [
            "unverified_address"
        ]

    def test_late_request_notice_goes_to_the_replaced_address(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        """The change was confirmed before the requested notice ran."""
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        change_email(client, tokens.access, NEW_MAIL)
        token = captured_emails.latest_token(NEW_MAIL, "change_email")
        verify_email(client, token)
        captured_emails.clear()

        run_task(client, send_email_change_requested_notice_task, _change_token_id(db))

        [notice] = captured_emails
        assert (notice.kind, notice.to) == ("email_change_requested", OLD_MAIL)

    def test_completed_notice_needs_a_confirmed_change(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        change_email(client, tokens.access, NEW_MAIL)
        captured_emails.clear()
        caplog.set_level(logging.INFO)

        run_task(client, send_email_change_completed_notice_task, _change_token_id(db))

        assert captured_emails == []
        assert _not_sent_reasons(caplog, "email_change_completed") == ["not_completed"]

    @pytest.mark.parametrize("notice", ["requested", "completed"])
    def test_inactive_account_gets_no_notice(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        notice: str,
    ) -> None:
        task = {
            "requested": send_email_change_requested_notice_task,
            "completed": send_email_change_completed_notice_task,
        }[notice]
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        change_email(client, tokens.access, NEW_MAIL)
        token = captured_emails.latest_token(NEW_MAIL, "change_email")
        verify_email(client, token)
        db.execute("UPDATE users SET is_active = false")
        captured_emails.clear()
        caplog.set_level(logging.INFO)

        run_task(client, task, _change_token_id(db))

        assert captured_emails == []
        reasons = [e["reason"] for e in log_events(caplog, "email.not_sent")]
        assert reasons == ["account_inactive"]

    def test_admin_locked_account_still_gets_the_notice(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        """Only an inactive account is skipped: a locked owner should know."""
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        change_email(client, tokens.access, NEW_MAIL)
        db.execute("UPDATE users SET is_locked = true, locked_until = NULL")
        captured_emails.clear()

        run_task(client, send_email_change_requested_notice_task, _change_token_id(db))

        [notice] = captured_emails
        assert notice.to == OLD_MAIL

    def test_enqueue_failure_keeps_the_answers(
        self, client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        caplog.set_level(logging.INFO)

        async def _kick(_message: BrokerMessage) -> Never:
            raise ConnectionError("broker down")

        app_of(client).state.broker.kick = _kick
        assert change_email(client, tokens.access, NEW_MAIL).status_code == 202
        db.execute(
            "UPDATE email_tokens SET token_hash = :h WHERE id = :t",
            h=hash_token("known"),
            t=_change_token_id(db),
        )
        r = verify_email(client, "known")

        assert r.status_code == 204
        assert db.scalar("SELECT email FROM users") == NEW_MAIL
        purposes = [
            e["purpose"] for e in log_events(caplog, "auth.email_enqueue_failed")
        ]
        assert purposes == [
            "change_email",
            "email_change_requested",
            "email_change_completed",
        ]


class TestRedisHoldsOnlyIds:
    @staticmethod
    def _stream_messages() -> list[dict[str, Any]]:
        """The task messages in the stream; Any: decoded JSON."""
        messages: list[dict[str, Any]] = []
        with redis_from_env("TASKIQ_BROKER_URL") as r:
            for _id, fields in r.xrange(TASK_STREAM) or []:
                assert fields is not None
                messages.append(json.loads(fields[b"data"]))
        return messages

    def test_lock_notice_carries_only_the_user_id(
        self, redis_broker_client: TestClient, db: Db
    ) -> None:
        register_ok(redis_broker_client, username=PII_USERNAME, email=PII_EMAIL)

        _lock_by_login(redis_broker_client, PII_USERNAME)

        [message] = [
            m
            for m in self._stream_messages()
            if m["task_name"] == "email.send_account_locked_notice"
        ]
        assert message["args"] == [user_id_of(db, PII_USERNAME)]
        assert message["kwargs"] == {}
        assert_nothing_personal_in_redis()

    def test_email_change_notices_carry_only_the_token_id(
        self, redis_broker_client: TestClient, db: Db
    ) -> None:
        new_mail = "zebra.new91@example.com"
        register_ok(redis_broker_client, username=PII_USERNAME, email=PII_EMAIL)
        tokens = login_tokens(redis_broker_client, PII_USERNAME)
        change_email(redis_broker_client, tokens.access, new_mail)
        token_id = _change_token_id(db, PII_USERNAME)
        db.execute(
            "UPDATE email_tokens SET token_hash = :h WHERE id = :t",
            h=hash_token("known"),
            t=token_id,
        )
        r = verify_email(redis_broker_client, "known")
        assert r.status_code == 204

        notices = {
            m["task_name"]: m
            for m in self._stream_messages()
            if m["task_name"].startswith("email.send_email_change_")
        }
        assert set(notices) == {
            "email.send_email_change_requested_notice",
            "email.send_email_change_completed_notice",
        }
        for message in notices.values():
            assert message["args"] == [token_id]
            assert message["kwargs"] == {}
        assert_nothing_personal_in_redis(new_mail)


class TestRelockAfterAnExpiredLock:
    """An expired timed lock counts as no lock.

    The next failures start a new series that locks again, records the lock
    and enqueues a notice.
    """

    @staticmethod
    def _expire_lock(db: Db) -> None:
        db.execute(
            "UPDATE users SET locked_until = now() - interval '1 second' "
            "WHERE username = 'alice'"
        )

    @staticmethod
    def _wrong_confirmation(client: TestClient, access: str) -> Response:
        return change_password(client, access, "wrong", "N3w-Password!")

    def _assert_locked_again(self, db: Db, caplog: pytest.LogCaptureFixture) -> None:
        row = db.one(
            "SELECT is_locked, failed_login_attempts, "
            "locked_until > now() AS lock_running FROM users"
        )
        assert row["is_locked"] is True
        assert row["lock_running"] is True
        assert row["failed_login_attempts"] == MAXIMUM_LOGIN_ATTEMPTS
        assert audit_count(db, "account_locked", "alice") == 2
        # The second notice is enqueued and throttled by the first one.
        assert _not_sent_reasons(caplog, "account_locked") == ["recently_notified"]

    def test_password_confirmations_lock_again(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _register_verified(client, db)
        tokens = login_tokens(client, "alice")
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):
            self._wrong_confirmation(client, tokens.access)
        self._expire_lock(db)
        caplog.set_level(logging.INFO)

        r = self._wrong_confirmation(client, tokens.access)

        assert r.status_code == 400
        row = db.one("SELECT is_locked, failed_login_attempts, locked_until FROM users")
        assert (row["is_locked"], row["failed_login_attempts"]) == (False, 1)
        assert row["locked_until"] is None

        for _ in range(MAXIMUM_LOGIN_ATTEMPTS - 1):
            self._wrong_confirmation(client, tokens.access)

        self._assert_locked_again(db, caplog)
        assert len(captured_emails.of_kind("account_locked")) == 1

    @pytest.mark.usefixtures("captured_emails")
    def test_logins_lock_again(
        self,
        client: TestClient,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _register_verified(client, db)
        _lock_by_login(client)
        self._expire_lock(db)
        caplog.set_level(logging.INFO)

        _lock_by_login(client)

        self._assert_locked_again(db, caplog)

    def test_admin_lock_is_never_weakened(self, client: TestClient, db: Db) -> None:
        _register_verified(client, db)
        user_id = user_id_of(db, "alice")
        _admin(client, service.admin_lock_user, user_id)

        async def _fail() -> None:
            session: AsyncSession
            async with app_of(client).state.session_factory() as session:
                await crud.register_failed_login(
                    session, uuid.UUID(user_id), MAXIMUM_LOGIN_ATTEMPTS, 900
                )

        for _ in range(MAXIMUM_LOGIN_ATTEMPTS + 1):
            call_in_app(client, _fail)

        row = db.one("SELECT is_locked, locked_until FROM users")
        assert (row["is_locked"], row["locked_until"]) == (True, None)
