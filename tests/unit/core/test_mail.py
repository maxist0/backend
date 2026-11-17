"""Mail configuration from Settings, built lazily."""

import asyncio

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

import app.core.mail as mail
from app.core.config import get_settings
from app.core.tasks import TransientTaskError

MAIL = {
    "MAIL_USERNAME": "mailer",
    "MAIL_PASSWORD": "smtp-secret-value-7f3a",
    "MAIL_FROM": "noreply@example.com",
    "MAIL_SERVER": "smtp.example.com",
    "MAIL_PORT": 2525,
    "MAIL_STARTTLS": False,
}


@pytest.fixture
def mail_settings(settings_factory):
    """Settings with a complete mail configuration, overridable per test."""

    def _build(**overrides):
        return settings_factory(**{**MAIL, **overrides})

    return _build


@pytest.fixture
def fresh_mailer():
    mail.get_mailer.cache_clear()
    yield
    mail.get_mailer.cache_clear()


class TestMailConfig:
    def test_config_comes_from_settings(self, mail_settings):
        config = mail.build_mail_config(mail_settings())

        assert config is not None
        assert config.MAIL_USERNAME == "mailer"
        assert config.MAIL_PASSWORD.get_secret_value() == MAIL["MAIL_PASSWORD"]
        assert config.MAIL_FROM == "noreply@example.com"
        assert config.MAIL_SERVER == "smtp.example.com"
        assert config.MAIL_PORT == 2525
        assert config.MAIL_STARTTLS is False
        # Templates are rendered by `render_email`, not by fastapi-mail.
        assert config.TEMPLATE_FOLDER is None

    def test_mail_password_is_not_exposed(self, mail_settings):
        settings = mail_settings()
        config = mail.build_mail_config(settings)

        text = repr(settings) + str(settings) + repr(config)

        assert MAIL["MAIL_PASSWORD"] not in text

    @pytest.mark.parametrize(
        "missing", ["MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM", "MAIL_SERVER"]
    )
    def test_incomplete_configuration_yields_none(self, mail_settings, missing):
        assert mail.build_mail_config(mail_settings(**{missing: None})) is None

    def test_empty_password_counts_as_missing(self, mail_settings):
        assert mail.build_mail_config(mail_settings(MAIL_PASSWORD="")) is None

    def test_env_local_values_reach_the_mail_config(self, fresh_settings, tmp_path):
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
    def test_mailer_is_built_on_first_use_and_cached(
        self, monkeypatch, fresh_mailer, mail_settings
    ):
        monkeypatch.setattr(mail, "get_settings", lambda: mail_settings())

        first = mail.get_mailer()

        assert isinstance(first, FastMail)
        assert mail.get_mailer() is first

    def test_unconfigured_mailer_is_none(
        self, monkeypatch, fresh_mailer, mail_settings
    ):
        monkeypatch.setattr(
            mail, "get_settings", lambda: mail_settings(MAIL_SERVER=None)
        )

        assert mail.get_mailer() is None

    def test_sending_without_configuration_reports_nothing_sent(self, monkeypatch):
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
        self, monkeypatch, tmp_path, mail_settings
    ):
        """The module's template is rendered with the core layout and sent as HTML."""
        (tmp_path / "probe").mkdir()
        (tmp_path / "probe" / "hello.html").write_text(
            '{% extends "base.html" %}{% block content %}Hi {{ username }}{% endblock %}',
            encoding="utf-8",
        )
        sent = []

        class _Mailer:
            async def send_message(self, message, template_name=None):
                sent.append((message, template_name))

        monkeypatch.setattr(mail, "get_mailer", lambda: _Mailer())
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
        assert "Hi &lt;b&gt;zed&lt;/b&gt;" in message.body
        assert "<html" in message.body.lower()


def _connection_error(cause: BaseException) -> Exception:
    """Wrap like fastapi-mail: ConnectionErrors raised inside the `except`."""
    try:
        raise cause
    except Exception as error:
        try:
            raise ConnectionErrors(f"Exception raised {error}")  # noqa: B904
        except ConnectionErrors as wrapped:
            return wrapped


class TestMailDeliveryError:
    """Only transient causes become the retried `TransientMailDeliveryError`."""

    @pytest.mark.parametrize(
        "cause",
        [
            pytest.param(lambda: SMTPServerDisconnected("gone"), id="disconnected"),
            pytest.param(lambda: SMTPReadTimeoutError("slow"), id="read-timeout"),
            pytest.param(lambda: TimeoutError(), id="timeout"),
            pytest.param(lambda: ConnectionResetError(), id="connection-reset"),
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
    def test_transient_causes_are_retried(self, cause):
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
    def test_permanent_causes_are_not_retried(self, cause):
        error = mail.mail_delivery_error(cause())

        assert type(error) is mail.MailDeliveryError
        assert not isinstance(error, TransientTaskError)

    def test_unreachable_smtp_server_is_transient(self):
        """The real fastapi-mail connect path: nothing listens on port 1."""
        config = ConnectionConfig(
            MAIL_USERNAME="mailer",
            MAIL_PASSWORD="smtp-secret-value-7f3a",
            MAIL_FROM="noreply@example.com",
            MAIL_SERVER="127.0.0.1",
            MAIL_PORT=1,
            MAIL_STARTTLS=False,
            MAIL_SSL_TLS=False,
            TIMEOUT=5,
        )
        message = MessageSchema(
            subject="s",
            recipients=["a@example.com"],
            body="b",
            subtype=MessageType.plain,
        )

        with pytest.raises(ConnectionErrors) as raised:
            asyncio.run(FastMail(config).send_message(message))

        error = mail.mail_delivery_error(raised.value)
        assert isinstance(error, mail.TransientMailDeliveryError)

    def test_message_is_only_the_type_of_the_cause(self):
        """SMTP replies can echo the recipient; taskiq logs the message."""
        cause = SMTPRecipientsRefused(
            [SMTPRecipientRefused(450, "a@example.com busy", "a@example.com")]
        )

        error = mail.mail_delivery_error(cause)

        assert str(error) == "SMTPRecipientsRefused"
