"""Token emails: re-keyed in the worker, nothing personal in Redis.

The request stores the token row with the hash of a value that is never
sent and enqueues the token's id. The mail task gives that token a fresh
value, if it is still open, and mails it. These tests run the real task: in-process for
the `client` fixture, through the Redis stream and the retry middleware
for the transport test.
"""

import json
import logging
import uuid
from collections.abc import Sequence
from typing import Never

import pytest
from fastapi.testclient import TestClient
from fastapi_mail import MessageSchema
from sqlalchemy import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import BrokerMessage
from taskiq.receiver import Receiver

from app.core.config import get_settings
from app.core.security import hash_token
from app.core.tasks import (
    RETRY_SCHEDULE_PREFIX,
    TASK_STREAM,
    build_broker,
    build_retry_middleware,
    build_retry_source,
)
from app.modules.auth import crud, service
from app.modules.auth.models import EmailToken
from app.modules.auth.tasks import send_token_email_task
from tests.helpers import (
    PASSWORD,
    PII_EMAIL,
    PII_USERNAME,
    CapturedEmail,
    CapturedEmails,
    Db,
    app_of,
    assert_nothing_personal_in_redis,
    bearer,
    call_in_app,
    change_email,
    log_events,
    login_tokens,
    redis_from_env,
    redis_keys,
    register_ok,
    reset_password,
    run_async,
    run_task,
    user_id_of,
    verify_email,
)

ENQUEUE_FAILED = "auth.email_enqueue_failed"


def _open_tokens(db: Db, username: str, purpose: str) -> Sequence[RowMapping]:
    return db.rows(
        "SELECT t.* FROM email_tokens t JOIN users u ON u.id = t.user_id "
        "WHERE u.username = :u AND lower(t.purpose::text) = :p "
        "AND t.used_at IS NULL AND t.invalidated_at IS NULL",
        u=username,
        p=purpose,
    )


def _latest_token_id(db: Db, username: str, purpose: str) -> str:
    """Id of the user's newest token of `purpose`, open or not."""
    return str(
        db.scalar(
            "SELECT t.id FROM email_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE u.username = :u AND lower(t.purpose::text) = :p "
            "ORDER BY t.created_at DESC LIMIT 1",
            u=username,
            p=purpose,
        )
    )


class TestRedisHoldsNothingPersonal:
    def test_stream_entry_and_retry_schedule_carry_only_the_token_id(
        self, redis_broker_client: TestClient, db: Db, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        register_ok(redis_broker_client, username=PII_USERNAME, email=PII_EMAIL)
        [placeholder] = _open_tokens(db, PII_USERNAME, "verify_email")
        token_id = str(placeholder["id"])

        # Before any worker consumed it (acknowledged entries are deleted).
        with redis_from_env("TASKIQ_BROKER_URL") as r:
            entries = r.xrange(TASK_STREAM)
        assert entries
        [(_entry_id, fields)] = entries
        assert fields is not None
        message = json.loads(fields[b"data"])
        assert message["task_name"] == "email.send_token_email"
        assert message["args"] == [token_id]
        assert message["kwargs"] == {}
        assert_nothing_personal_in_redis(placeholder["token_hash"])

        attempted: list[CapturedEmail] = []

        class _TimingOutMailer:
            async def send_message(self, message: MessageSchema) -> None:
                attempted.append(CapturedEmail.from_message(message))
                raise TimeoutError()

        mailer = _TimingOutMailer()
        monkeypatch.setattr("app.core.mail.get_mailer", lambda: mailer)
        monkeypatch.setattr("app.modules.auth.tasks.get_mailer", lambda: mailer)

        async def _worker_takes_the_entry() -> None:
            settings = get_settings()
            broker = build_broker(settings)
            broker.add_middlewares(
                build_retry_middleware(settings, build_retry_source(settings))
            )
            broker.is_worker_process = True
            await broker.startup()
            try:
                listener = broker.listen()
                delivered = await anext(listener)
                await listener.aclose()
                await Receiver(broker).callback(delivered)
            finally:
                await broker.shutdown()

        run_async(_worker_takes_the_entry())

        [mail] = attempted
        assert mail.to == PII_EMAIL
        with redis_from_env("TASKIQ_BROKER_URL") as r:
            [data_key] = redis_keys(r, f"{RETRY_SCHEDULE_PREFIX}:data:*")
            raw_schedule = r.get(data_key)
            assert raw_schedule is not None
            schedule = json.loads(raw_schedule)
            assert r.xlen(TASK_STREAM) == 0
        assert schedule["task_name"] == "email.send_token_email"
        assert schedule["args"] == [token_id]
        assert schedule["kwargs"] == {}
        assert mail.token
        assert_nothing_personal_in_redis(mail.token, hash_token(mail.token))


class TestRekeying:
    def test_each_run_mails_a_new_token_and_only_the_last_one_redeems(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        first = captured_emails.latest_token("alice@example.com")

        run_task(
            client, send_token_email_task, _latest_token_id(db, "alice", "verify_email")
        )

        second = captured_emails.latest_token("alice@example.com")
        assert len(captured_emails.of_kind("verify")) == 2
        assert first != second
        r = verify_email(client, first)
        assert r.status_code == 400
        r = verify_email(client, second)
        assert r.status_code == 204

    def test_request_stores_a_hash_that_no_mail_carries(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The worker re-keys: the request's own hash is never redeemable."""
        hashes: list[str] = []
        real_create = crud.create_email_token

        async def _spy(
            db_session: AsyncSession, token: EmailToken, *, commit: bool = True
        ) -> EmailToken:
            hashes.append(token.token_hash)
            return await real_create(db_session, token, commit=commit)

        monkeypatch.setattr(crud, "create_email_token", _spy)
        register_ok(client, username="alice")

        [token] = _open_tokens(db, "alice", "verify_email")
        mail_token = captured_emails.latest_token("alice@example.com")
        assert token["token_hash"] == hash_token(mail_token)
        assert hashes
        assert token["token_hash"] not in hashes

    @pytest.mark.usefixtures("captured_emails")
    def test_expiry_stays_as_the_request_set_it(
        self, client: TestClient, db: Db
    ) -> None:
        register_ok(client, username="alice")
        [before] = _open_tokens(db, "alice", "verify_email")

        run_task(
            client, send_token_email_task, _latest_token_id(db, "alice", "verify_email")
        )

        [after] = _open_tokens(db, "alice", "verify_email")
        assert after["expires_at"] == before["expires_at"]
        assert after["token_hash"] != before["token_hash"]

    def test_change_email_goes_to_the_new_address_from_the_token(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        tokens = login_tokens(client, "alice")
        r = change_email(client, tokens.access, "new@example.com")
        assert r.status_code == 202

        [mail] = captured_emails.of_kind("change_email")
        assert (mail.to, mail.username) == ("new@example.com", "alice")


class TestOneTokenPerMail:
    """Each task keys its own token, and a redemption ends the other one.

    Parallel requests can leave two open tokens of one purpose.
    """

    @staticmethod
    def _second_open_token(db: Db, username: str, purpose: str) -> str:
        """Copy the user's open token of `purpose` as an older, open row."""
        token_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO email_tokens (id, user_id, purpose, token_hash, "
            "new_email, expires_at, created_at) "
            "SELECT :id, user_id, purpose, 'x', new_email, expires_at, "
            "created_at - interval '1 second' FROM email_tokens "
            "WHERE id = :latest",
            id=token_id,
            latest=_latest_token_id(db, username, purpose),
        )
        return token_id

    def test_task_keys_only_its_own_token(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        older = self._second_open_token(db, "alice", "verify_email")
        newest = _latest_token_id(db, "alice", "verify_email")
        captured_emails.clear()

        run_task(client, send_token_email_task, older)

        [mail] = captured_emails
        hashes = {
            str(row["id"]): row["token_hash"]
            for row in _open_tokens(db, "alice", "verify_email")
        }
        assert mail.token
        assert hashes[older] == hash_token(mail.token)
        assert hashes[newest] != hash_token(mail.token)

    def test_redeeming_one_token_ends_the_other_open_one(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        older = self._second_open_token(db, "alice", "verify_email")
        token = captured_emails.latest_token("alice@example.com")

        r = verify_email(client, token)

        assert r.status_code == 204
        assert _open_tokens(db, "alice", "verify_email") == []
        captured_emails.clear()
        run_task(client, send_token_email_task, older)
        assert captured_emails == []

    def test_redeeming_a_reset_token_ends_the_other_open_one(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})
        self._second_open_token(db, "alice", "reset_password")
        token = captured_emails.latest_token("alice@example.com", "reset")

        r = reset_password(client, token, "N3w-Password!")

        assert r.status_code == 204
        assert _open_tokens(db, "alice", "reset_password") == []


class TestNothingToSend:
    def test_stale_retry_after_expiry_sends_nothing(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        register_ok(client, username="alice")
        captured_emails.clear()
        token_id = _latest_token_id(db, "alice", "verify_email")
        db.execute(
            "UPDATE email_tokens SET expires_at = now() - interval '1 second' "
            "WHERE id = :t",
            t=token_id,
        )
        caplog.set_level(logging.INFO)

        run_task(
            client, send_token_email_task, _latest_token_id(db, "alice", "verify_email")
        )

        assert captured_emails == []
        [event] = log_events(caplog, "email.not_sent")
        assert event["reason"] == "no_open_token"
        assert event["token_id"] == token_id

    def test_token_cancelled_by_a_password_change_is_not_mailed(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        tokens = login_tokens(client, "alice")
        r = client.post(
            "/api/v1/auth/password/forgot", json={"email": "alice@example.com"}
        )
        assert r.status_code == 202
        r = client.put(
            "/api/v1/users/me/password",
            json={"current_password": PASSWORD, "new_password": "N3w-Password!"},
            headers=bearer(tokens.access),
        )
        assert r.status_code == 204
        captured_emails.clear()

        run_task(
            client,
            send_token_email_task,
            _latest_token_id(db, "alice", "reset_password"),
        )
        run_task(
            client, send_token_email_task, _latest_token_id(db, "alice", "verify_email")
        )

        assert captured_emails == []

    def test_token_cancelled_by_an_admin_lock_is_not_mailed(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        register_ok(client, username="alice")
        captured_emails.clear()
        user_id = user_id_of(db, "alice")

        async def _admin_lock() -> None:
            async with app_of(client).state.session_factory() as session:
                assert await service.admin_lock_user(
                    session, uuid.UUID(user_id), actor="admin"
                )

        call_in_app(client, _admin_lock)
        run_task(
            client, send_token_email_task, _latest_token_id(db, "alice", "verify_email")
        )

        assert captured_emails == []

    @pytest.mark.parametrize(
        "block", ["is_active = false", "is_locked = true, locked_until = NULL"]
    )
    def test_blocked_account_gets_no_mail_and_its_token_ends(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        block: str,
    ) -> None:
        """A token left open for a blocked account is not mailed; it ends.

        Such a token comes from a request that raced the block.
        """
        register_ok(client, username="alice")
        captured_emails.clear()
        user_id = user_id_of(db, "alice")
        db.execute(f"UPDATE users SET {block} WHERE id = :u", u=user_id)
        caplog.set_level(logging.INFO)

        run_task(
            client, send_token_email_task, _latest_token_id(db, "alice", "verify_email")
        )

        assert captured_emails == []
        assert _open_tokens(db, "alice", "verify_email") == []
        [event] = log_events(caplog, "email.not_sent")
        assert event["reason"] == "account_blocked"

    def test_verified_address_gets_no_verification_mail(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        register_ok(client, username="alice")
        captured_emails.clear()
        user_id = user_id_of(db, "alice")
        db.execute("UPDATE users SET is_email_verified = true WHERE id = :u", u=user_id)
        caplog.set_level(logging.INFO)

        run_task(
            client, send_token_email_task, _latest_token_id(db, "alice", "verify_email")
        )

        assert captured_emails == []
        assert _open_tokens(db, "alice", "verify_email") == []
        [event] = log_events(caplog, "email.not_sent")
        assert event["reason"] == "already_verified"


class TestMailNotConfigured:
    def test_task_logs_one_clear_event_and_leaves_the_token(
        self,
        client: TestClient,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No `captured_emails`: the test process has no mail configuration."""

        async def _no_rekey(*_args: object) -> Never:
            raise AssertionError("the token must not be re-keyed")

        # A call would fail the task, and the `client` fixture the test.
        monkeypatch.setattr(service, "prepare_token_email", _no_rekey)
        caplog.set_level(logging.INFO)

        register_ok(client, username="alice")

        [event] = log_events(caplog, "email.not_sent")
        assert event["reason"] == "mail_not_configured"
        assert event["token_id"] == _latest_token_id(db, "alice", "verify_email")
        assert log_events(caplog, "email.sent") == []
        assert len(_open_tokens(db, "alice", "verify_email")) == 1


class TestEnqueueFailure:
    """A broker failure after the commit answers as on success and is logged.

    The token row is committed before the enqueue; the log names the error
    type only.
    """

    @staticmethod
    def _break_broker(client: TestClient) -> None:
        async def _kick(_message: BrokerMessage) -> Never:
            raise ConnectionError("broker down")

        app_of(client).state.broker.kick = _kick

    def _assert_logged(
        self, caplog: pytest.LogCaptureFixture, db: Db, username: str, purpose: str
    ) -> None:
        [event] = log_events(caplog, ENQUEUE_FAILED)
        assert event["user_id"] == user_id_of(db, username)
        assert event["purpose"] == purpose
        assert event["error_type"] == "ConnectionError"
        assert len(_open_tokens(db, username, purpose)) == 1

    def test_register_answers_201(
        self, client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        self._break_broker(client)

        r = client.post(
            "/api/v1/auth/register",
            json={"username": "alice", "email": "a@example.com", "password": PASSWORD},
        )

        assert r.status_code == 201
        self._assert_logged(caplog, db, "alice", "verify_email")

    def test_forgot_password_answers_202(
        self, client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        register_ok(client, username="alice")
        caplog.set_level(logging.INFO)
        self._break_broker(client)

        r = client.post(
            "/api/v1/auth/password/forgot", json={"email": "alice@example.com"}
        )

        assert r.status_code == 202
        self._assert_logged(caplog, db, "alice", "reset_password")

    def test_resend_and_email_change_answer_202(
        self, client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        register_ok(client, username="alice")
        tokens = login_tokens(client, "alice")
        caplog.set_level(logging.INFO)
        self._break_broker(client)

        r = client.post(
            "/api/v1/auth/email/verify/resend", headers=bearer(tokens.access)
        )
        assert r.status_code == 202
        r = change_email(client, tokens.access, "new@example.com")
        assert r.status_code == 202

        events = log_events(caplog, ENQUEUE_FAILED)
        assert [e["purpose"] for e in events] == [
            "verify_email",
            "change_email",
            "email_change_requested",
        ]
        assert len(_open_tokens(db, "alice", "verify_email")) == 1
        assert len(_open_tokens(db, "alice", "change_email")) == 1


class TestForgotPasswordForBlockedAccounts:
    @pytest.mark.parametrize(
        ("block", "detail"),
        [
            ("is_active = false", "No email sent: account inactive"),
            ("is_locked = true, locked_until = NULL", "No email sent: account locked"),
        ],
    )
    def test_answers_202_without_token_or_mail(
        self,
        client: TestClient,
        captured_emails: CapturedEmails,
        db: Db,
        block: str,
        detail: str,
    ) -> None:
        register_ok(client, username="alice")
        captured_emails.clear()
        db.execute(f"UPDATE users SET {block} WHERE username = 'alice'")

        r = client.post(
            "/api/v1/auth/password/forgot", json={"email": "alice@example.com"}
        )

        assert r.status_code == 202
        assert r.content == b""
        assert captured_emails == []
        assert _open_tokens(db, "alice", "reset_password") == []
        row = db.one(
            "SELECT detail FROM auth_audit_log "
            "WHERE lower(event_type::text) = 'password_reset_requested'"
        )
        assert row["detail"] == detail

    def test_timed_lock_still_gets_a_reset_link(
        self, client: TestClient, captured_emails: CapturedEmails, db: Db
    ) -> None:
        """Only the admin lock blocks; a lock from failed logins does not."""
        register_ok(client, username="alice")
        db.execute(
            "UPDATE users SET is_locked = true, "
            "locked_until = now() + interval '10 minutes' WHERE username = 'alice'"
        )

        r = client.post(
            "/api/v1/auth/password/forgot", json={"email": "alice@example.com"}
        )

        assert r.status_code == 202
        assert len(captured_emails.of_kind("reset")) == 1


class TestMailLinks:
    def test_links_are_absolute_frontend_urls(
        self, client: TestClient, captured_emails: CapturedEmails
    ) -> None:
        register_ok(client, username="alice")
        client.post("/api/v1/auth/password/forgot", json={"email": "alice@example.com"})

        verify = captured_emails.of_kind("verify")[-1]
        reset = captured_emails.of_kind("reset")[-1]
        assert verify.link == (
            f"http://localhost:5173/user/email-bestaetigen?token={verify.token}"
        )
        assert reset.link == (
            f"http://localhost:5173/user/passwort-zuruecksetzen?token={reset.token}"
        )
        assert verify.username == "alice"
