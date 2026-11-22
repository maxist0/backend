"""The mail task logs one clear outcome, never the recipient, username or token."""

import asyncio
import logging
import traceback
import uuid
from typing import Never, cast

import pytest
from aiosmtplib import SMTPRecipientRefused, SMTPRecipientsRefused
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.mail import MailDeliveryError
from app.modules.auth import service
from app.modules.auth import tasks as email_tasks
from app.modules.auth.models import EmailTokenPurpose
from tests.helpers import (
    PII_EMAIL,
    PII_USERNAME,
    log_events,
    log_text,
)

TOKEN = "tok-123"
USER_ID = uuid.uuid4()
TOKEN_ID = str(uuid.uuid4())
# The task passes its session only to `prepare_token_email`, which is replaced.
_NO_SESSION = cast("AsyncSession", None)


class _Prepared:
    """What the replaced service returns, and the token ids it was asked for."""

    def __init__(self) -> None:
        self.calls: list[uuid.UUID] = []
        self.purpose = EmailTokenPurpose.VERIFY_EMAIL


@pytest.fixture
def prepared(monkeypatch: pytest.MonkeyPatch) -> _Prepared:
    """The service hands the task the PII markers and records each call."""
    state = _Prepared()

    async def _prepare(
        _db: AsyncSession, token_id: uuid.UUID
    ) -> service.TokenEmail | None:
        state.calls.append(token_id)
        return service.TokenEmail(
            user_id=USER_ID,
            purpose=state.purpose,
            recipient=PII_EMAIL,
            username=PII_USERNAME,
            token=TOKEN,
        )

    monkeypatch.setattr(service, "prepare_token_email", _prepare)
    return state


def _mail_configured(
    monkeypatch: pytest.MonkeyPatch, *, configured: bool = True
) -> None:
    monkeypatch.setattr(
        email_tasks, "get_mailer", lambda: object() if configured else None
    )


def _run(prepared: _Prepared, purpose: EmailTokenPurpose) -> None:
    prepared.purpose = purpose
    asyncio.run(email_tasks.send_token_email_task.original_func(TOKEN_ID, _NO_SESSION))


class TestLoggingHygiene:
    @pytest.mark.parametrize("outcome", ["sent", "failed"])
    @pytest.mark.parametrize("purpose", list(EmailTokenPurpose))
    def test_task_does_not_log_recipient_username_or_token(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        prepared: _Prepared,
        purpose: EmailTokenPurpose,
        outcome: str,
    ) -> None:
        async def _send(**_kwargs: object) -> bool:
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

        text = log_text(caplog)
        assert caplog.records
        assert PII_EMAIL not in text
        assert PII_USERNAME not in text
        assert TOKEN not in text
        expected = "email.sent" if outcome == "sent" else "email.send_failed"
        [event] = [e for e in log_events(caplog) if e["event"].startswith("email.")]
        assert event["event"] == expected
        assert (event["token_id"], event["user_id"], event["purpose"]) == (
            TOKEN_ID,
            str(USER_ID),
            purpose.value,
        )


class TestMailNotConfigured:
    def test_one_clear_event_and_the_token_is_not_touched(
        self,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        prepared: _Prepared,
    ) -> None:
        async def _send(**_kwargs: object) -> Never:
            raise AssertionError("nothing may be rendered or sent")

        _mail_configured(monkeypatch, configured=False)
        monkeypatch.setattr(email_tasks, "send_templated_email", _send)
        caplog.set_level(logging.DEBUG)

        _run(prepared, EmailTokenPurpose.VERIFY_EMAIL)

        assert prepared.calls == []  # no re-key
        [event] = [e for e in log_events(caplog) if e["event"].startswith("email.")]
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
        self,
        monkeypatch: pytest.MonkeyPatch,
        prepared: _Prepared,
        purpose: EmailTokenPurpose,
    ) -> None:
        async def _send(**_kwargs: object) -> Never:
            refused = SMTPRecipientRefused(
                550, f"<{PII_EMAIL}> unknown user", PII_EMAIL
            )
            raise SMTPRecipientsRefused([refused])

        _mail_configured(monkeypatch)
        monkeypatch.setattr(email_tasks, "send_templated_email", _send)

        with pytest.raises(MailDeliveryError) as raised:
            _run(prepared, purpose)

        rendered = "".join(traceback.format_exception(raised.value))
        assert str(raised.value) == "SMTPRecipientsRefused"
        assert PII_EMAIL not in rendered
        assert TOKEN not in rendered
