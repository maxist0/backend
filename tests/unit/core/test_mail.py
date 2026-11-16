"""Mail configuration from Settings, built lazily."""

import asyncio

import pytest
from fastapi_mail import FastMail, MessageType

import app.core.mail as mail
from app.core.config import get_settings

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

    def test_sending_without_configuration_is_skipped(self, monkeypatch):
        monkeypatch.setattr(mail, "get_mailer", lambda: None)

        asyncio.run(
            mail.send_templated_email(
                to="someone@example.com",
                subject="s",
                template_name="verify_email.html",
                context={},
            )
        )

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

        asyncio.run(
            mail.send_templated_email(
                to="someone@example.com",
                subject="s",
                template_name="probe/hello.html",
                context={"username": "<b>zed</b>"},
                template_dir=tmp_path,
            )
        )

        ((message, template_name),) = sent
        assert template_name is None
        assert message.subtype is MessageType.html
        assert "Hi &lt;b&gt;zed&lt;/b&gt;" in message.body
        assert "<html" in message.body.lower()
