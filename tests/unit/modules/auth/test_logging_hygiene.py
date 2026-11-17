"""The mail task logs one clear outcome, never the recipient, username or token."""

import asyncio
import logging
import traceback
import uuid
from types import SimpleNamespace

import pytest

from app.core.mail import MailDeliveryError
from app.modules.auth import service
from app.modules.auth import tasks as email_tasks
from app.modules.auth.models import EmailTokenPurpose

PII_USER = "zebra_user_91"
PII_MAIL = "zebra.mail91@example.com"
TOKEN = "tok-123"
USER_ID = uuid.uuid4()
TOKEN_ID = str(uuid.uuid4())


def _log_text(caplog) -> str:
    return "\n".join(f"{r.name} {r.msg} {r.getMessage()}" for r in caplog.records)


def _events(caplog) -> list[dict]:
    return [r.msg for r in caplog.records if isinstance(r.msg, dict)]


@pytest.fixture
def prepared(monkeypatch) -> SimpleNamespace:
    """The service hands the task the PII markers and records each call."""
    state = SimpleNamespace(calls=[], purpose=EmailTokenPurpose.VERIFY_EMAIL)

    async def _prepare(db, token_id):
        state.calls.append(token_id)
        return service.TokenEmail(
            user_id=USER_ID,
            purpose=state.purpose,
            recipient=PII_MAIL,
            username=PII_USER,
            token=TOKEN,
        )

    monkeypatch.setattr(service, "prepare_token_email", _prepare)
    return state


def _mail_configured(monkeypatch, configured: bool = True) -> None:
    monkeypatch.setattr(
        email_tasks, "get_mailer", lambda: object() if configured else None
    )


def _run(prepared: SimpleNamespace, purpose: EmailTokenPurpose) -> None:
    prepared.purpose = purpose
    asyncio.run(email_tasks.send_token_email_task.original_func(TOKEN_ID, None))


class TestLoggingHygiene:
    @pytest.mark.parametrize("outcome", ["sent", "failed"])
    @pytest.mark.parametrize("purpose", list(EmailTokenPurpose))
    def test_task_does_not_log_recipient_username_or_token(
        self, monkeypatch, caplog, prepared, purpose, outcome
    ):
        async def _send(**_kwargs):
            if outcome == "failed":
                raise RuntimeError("smtp down")
            return True

        _mail_configured(monkeypatch)
        monkeypatch.setattr(email_tasks, "send_templated_email", _send)
        caplog.set_level(logging.DEBUG)

        try:
            _run(prepared, purpose)
        except MailDeliveryError:
            assert outcome == "failed"

        text = _log_text(caplog)
        assert caplog.records
        assert PII_MAIL not in text
        assert PII_USER not in text
        assert TOKEN not in text
        expected = "email.sent" if outcome == "sent" else "email.send_failed"
        [event] = [e for e in _events(caplog) if e["event"].startswith("email.")]
        assert event["event"] == expected
        assert (event["token_id"], event["user_id"], event["purpose"]) == (
            TOKEN_ID,
            str(USER_ID),
            purpose.value,
        )


class TestMailNotConfigured:
    def test_one_clear_event_and_the_token_is_not_touched(
        self, monkeypatch, caplog, prepared
    ):
        async def _send(**_kwargs):
            raise AssertionError("nothing may be rendered or sent")

        _mail_configured(monkeypatch, configured=False)
        monkeypatch.setattr(email_tasks, "send_templated_email", _send)
        caplog.set_level(logging.DEBUG)

        _run(prepared, EmailTokenPurpose.VERIFY_EMAIL)

        assert prepared.calls == []  # no re-key
        [event] = [e for e in _events(caplog) if e["event"].startswith("email.")]
        assert (event["event"], event["reason"], event["token_id"]) == (
            "email.not_sent",
            "mail_not_configured",
            TOKEN_ID,
        )


class TestMailTaskErrors:
    """A failed mail task raises an error that carries no SMTP text.

    taskiq logs the exception of a failed task with message and traceback,
    and SMTP errors can echo the recipient address.
    """

    @pytest.mark.parametrize("purpose", list(EmailTokenPurpose))
    def test_raised_error_has_only_the_original_type(
        self, monkeypatch, prepared, purpose
    ):
        class SMTPRecipientsRefused(Exception):
            pass

        async def _send(**_kwargs):
            raise SMTPRecipientsRefused(f"550 <{PII_MAIL}> unknown user")

        _mail_configured(monkeypatch)
        monkeypatch.setattr(email_tasks, "send_templated_email", _send)

        with pytest.raises(MailDeliveryError) as raised:
            _run(prepared, purpose)

        rendered = "".join(traceback.format_exception(raised.value))
        assert str(raised.value) == "SMTPRecipientsRefused"
        assert PII_MAIL not in rendered
        assert TOKEN not in rendered
