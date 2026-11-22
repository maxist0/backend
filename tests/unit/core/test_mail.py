"""Mail configuration from Settings, built lazily."""

import asyncio
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from aiosmtplib import (
    SMTPAuthenticationError,
    SMTPConnectError,
    SMTPConnectResponseError,
    SMTPDataError,
    SMTPReadTimeoutError,
    SMTPRecipientRefused,
    SMTPRecipientsRefused,
    SMTPResponseException,
    SMTPServerDisconnected,
)
from fastapi_mail import ConnectionConfig, FastMail, MessageSchema, MessageType
from fastapi_mail.errors import ConnectionErrors
from jinja2 import TemplateNotFound
from pydantic import NameEmail, SecretStr

from app.core import mail
from app.core.config import Settings, get_settings
from app.core.tasks import TransientTaskError
from tests.helpers import SettingsFactory

MAIL_PASSWORD = "smtp-secret-value-7f3a"
MAIL: dict[str, object] = {
    "MAIL_USERNAME": "mailer",
    "MAIL_PASSWORD": MAIL_PASSWORD,
    "MAIL_FROM": "noreply@example.com",
    "MAIL_SERVER": "smtp.example.com",
    "MAIL_PORT": 2525,
    "MAIL_STARTTLS": False,
}


@pytest.fixture
def mail_settings(settings_factory: SettingsFactory) -> SettingsFactory:
    """Settings with a complete mail configuration, overridable per test."""

    def _build(**overrides: object) -> Settings:
        return settings_factory(**{**MAIL, **overrides})

    return _build


@pytest.fixture
def fresh_mailer() -> Iterator[None]:
    mail.get_mailer.cache_clear()
    yield
    mail.get_mailer.cache_clear()


class TestMailConfig:
    def test_config_comes_from_settings(self, mail_settings: SettingsFactory) -> None:
        config = mail.build_mail_config(mail_settings())

        assert config is not None
        assert config.MAIL_USERNAME == "mailer"
        assert config.MAIL_PASSWORD.get_secret_value() == MAIL_PASSWORD
        assert config.MAIL_FROM == "noreply@example.com"
        assert config.MAIL_SERVER == "smtp.example.com"
        assert config.MAIL_PORT == 2525
        assert config.MAIL_STARTTLS is False
        # Templates are rendered by `render_email`, not by fastapi-mail.
        assert config.TEMPLATE_FOLDER is None

    def test_mail_password_is_not_exposed(self, mail_settings: SettingsFactory) -> None:
        settings = mail_settings()
        config = mail.build_mail_config(settings)

        text = repr(settings) + str(settings) + repr(config)

        assert MAIL_PASSWORD not in text

    @pytest.mark.parametrize(
        "missing", ["MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM", "MAIL_SERVER"]
    )
    def test_incomplete_configuration_yields_none(
        self, mail_settings: SettingsFactory, missing: str
    ) -> None:
        assert mail.build_mail_config(mail_settings(**{missing: None})) is None

    def test_empty_password_counts_as_missing(
        self, mail_settings: SettingsFactory
    ) -> None:
        assert mail.build_mail_config(mail_settings(MAIL_PASSWORD="")) is None

    def test_env_local_values_reach_the_mail_config(
        self, fresh_settings: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """MAIL_* in .env.local must reach the mail config (no os.getenv bypass)."""
        lines = [f"{key}={value}" for key, value in MAIL.items()]
        (tmp_path / ".env.local").write_text("\n".join(lines), encoding="utf-8")
        fresh_settings.chdir(tmp_path)
        fresh_settings.setenv("ENVIRONMENT", "dev")
        for key in MAIL:
            fresh_settings.delenv(key, raising=False)

        config = mail.build_mail_config(get_settings())

        assert config is not None
        assert config.MAIL_SERVER == "smtp.example.com"


class TestMailer:
    @pytest.mark.usefixtures("fresh_mailer")
    def test_mailer_is_built_on_first_use_and_cached(
        self, monkeypatch: pytest.MonkeyPatch, mail_settings: SettingsFactory
    ) -> None:
        monkeypatch.setattr(mail, "get_settings", mail_settings)

        first = mail.get_mailer()

        assert isinstance(first, FastMail)
        assert mail.get_mailer() is first

    @pytest.mark.usefixtures("fresh_mailer")
    def test_unconfigured_mailer_is_none(
        self, monkeypatch: pytest.MonkeyPatch, mail_settings: SettingsFactory
    ) -> None:
        monkeypatch.setattr(
            mail, "get_settings", lambda: mail_settings(MAIL_SERVER=None)
        )

        assert mail.get_mailer() is None

    def test_sending_without_configuration_reports_nothing_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(mail, "get_mailer", lambda: None)

        sent = asyncio.run(
            mail.send_templated_email(
                to="someone@example.com",
                subject="s",
                template_name="verify_email.html",
                context={},
            )
        )

        assert sent is False

    def test_sending_passes_the_rendered_html_body(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        mail_settings: SettingsFactory,
    ) -> None:
        """The module's template is rendered with the core layout and sent as HTML."""
        (tmp_path / "probe").mkdir()
        (tmp_path / "probe" / "hello.html").write_text(
            '{% extends "base.html" %}'
            "{% block content %}Hi {{ username }}{% endblock %}",
            encoding="utf-8",
        )
        sent: list[tuple[MessageSchema, str | None]] = []

        class _Mailer:
            async def send_message(
                self, message: MessageSchema, template_name: str | None = None
            ) -> None:
                sent.append((message, template_name))

        monkeypatch.setattr(mail, "get_mailer", _Mailer)
        monkeypatch.setattr(
            mail, "get_settings", lambda: mail_settings(APP_NAME="Probe")
        )

        outcome = asyncio.run(
            mail.send_templated_email(
                to="someone@example.com",
                subject="s",
                template_name="probe/hello.html",
                context={"username": "<b>zed</b>"},
                template_dir=tmp_path,
            )
        )

        assert outcome is True
        ((message, template_name),) = sent
        assert template_name is None
        assert message.subtype is MessageType.html
        body = message.body  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] - fastapi-mail types the body as `str | list`
        assert isinstance(body, str)
        assert "Hi &lt;b&gt;zed&lt;/b&gt;" in body
        assert "<html" in body.lower()


def _connection_error(cause: BaseException) -> Exception:
    """Wrap like fastapi-mail, which raises `ConnectionErrors` inside `except`.

    Raising inside an `except` block sets `__context__` to the handled error.
    """
    wrapped = ConnectionErrors(f"Exception raised {cause}")  # type: ignore[no-untyped-call]  # fastapi-mail's error class is unannotated
    wrapped.__context__ = cause
    return wrapped


class TestMailDeliveryError:
    """Only transient causes become the retried `TransientMailDeliveryError`."""

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(lambda: SMTPServerDisconnected("gone"), id="disconnected"),
            pytest.param(lambda: SMTPReadTimeoutError("slow"), id="read-timeout"),
            pytest.param(TimeoutError, id="timeout"),
            pytest.param(ConnectionResetError, id="connection-reset"),
            pytest.param(
                lambda: SMTPResponseException(451, "try later"), id="smtp-451"
            ),
            pytest.param(
                lambda: SMTPRecipientsRefused(
                    [SMTPRecipientRefused(452, "full", "a@example.com")]
                ),
                id="recipients-452",
            ),
            pytest.param(
                lambda: _connection_error(SMTPConnectError("refused")),
                id="connect-via-fastapi-mail",
            ),
            pytest.param(
                lambda: _connection_error(SMTPConnectResponseError(421, "busy")),
                id="greeting-421-via-fastapi-mail",
            ),
        ],
    )
    def test_transient_causes_are_retried(
        self, cause: Callable[[], BaseException]
    ) -> None:
        error = mail.mail_delivery_error(cause())

        assert isinstance(error, mail.TransientMailDeliveryError)
        assert isinstance(error, TransientTaskError)

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(
                lambda: SMTPRecipientsRefused(
                    [SMTPRecipientRefused(550, "no such user", "a@example.com")]
                ),
                id="recipients-550",
            ),
            pytest.param(lambda: SMTPDataError(554, "rejected"), id="smtp-554"),
            pytest.param(
                lambda: _connection_error(SMTPAuthenticationError(535, "bad login")),
                id="auth-535-via-fastapi-mail",
            ),
            pytest.param(
                lambda: _connection_error(ValueError("bad config")),
                id="non-network-via-fastapi-mail",
            ),
            pytest.param(lambda: TemplateNotFound("verify_email.html"), id="template"),
            pytest.param(lambda: TypeError("bad call"), id="type-error"),
            pytest.param(lambda: KeyError("username"), id="key-error"),
        ],
    )
    def test_permanent_causes_are_not_retried(
        self, cause: Callable[[], BaseException]
    ) -> None:
        error = mail.mail_delivery_error(cause())

        assert type(error) is mail.MailDeliveryError
        assert not isinstance(error, TransientTaskError)

    def test_unreachable_smtp_server_is_transient(self) -> None:
        """The real fastapi-mail connect path: nothing listens on port 1."""
        config = ConnectionConfig(
            MAIL_USERNAME="mailer",
            MAIL_PASSWORD=SecretStr(MAIL_PASSWORD),
            MAIL_FROM="noreply@example.com",
            MAIL_SERVER="127.0.0.1",
            MAIL_PORT=1,
            MAIL_STARTTLS=False,
            MAIL_SSL_TLS=False,
            TIMEOUT=5,
        )
        message = MessageSchema(
            subject="s",
            recipients=[NameEmail(name="a", email="a@example.com")],
            body="b",
            subtype=MessageType.plain,
        )

        with pytest.raises(ConnectionErrors) as raised:
            asyncio.run(FastMail(config).send_message(message))

        error = mail.mail_delivery_error(raised.value)
        assert isinstance(error, mail.TransientMailDeliveryError)

    def test_message_is_only_the_type_of_the_cause(self) -> None:
        """SMTP replies can echo the recipient; taskiq logs the message."""
        cause = SMTPRecipientsRefused(
            [SMTPRecipientRefused(450, "a@example.com busy", "a@example.com")]
        )

        error = mail.mail_delivery_error(cause)

        assert str(error) == "SMTPRecipientsRefused"
