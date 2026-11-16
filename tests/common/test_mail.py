# tests/common/test_mail.py
"""Mail configuration from Settings, built lazily (review 2026-11, BE-19)."""

import asyncio

import pytest
from fastapi_mail import FastMail

import app.common.mail as mail
from app.common.config import Settings, get_settings

MAIL = {
    "MAIL_USERNAME": "mailer",
    "MAIL_PASSWORD": "smtp-secret-value-7f3a",
    "MAIL_FROM": "noreply@example.com",
    "MAIL_SERVER": "smtp.example.com",
    "MAIL_PORT": 2525,
    "MAIL_STARTTLS": False,
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, ENVIRONMENT="unittest", **{**MAIL, **overrides})


@pytest.fixture
def fresh_mailer():
    mail.get_mailer.cache_clear()
    yield
    mail.get_mailer.cache_clear()


class TestMailConfig:
    def test_config_comes_from_settings(self):
        config = mail.build_mail_config(_settings())

        assert config is not None
        assert config.MAIL_USERNAME == "mailer"
        assert config.MAIL_PASSWORD.get_secret_value() == MAIL["MAIL_PASSWORD"]
        assert config.MAIL_FROM == "noreply@example.com"
        assert config.MAIL_SERVER == "smtp.example.com"
        assert config.MAIL_PORT == 2525
        assert config.MAIL_STARTTLS is False
        assert config.TEMPLATE_FOLDER == mail.TEMPLATE_FOLDER

    def test_mail_password_is_not_exposed(self):
        settings = _settings()
        config = mail.build_mail_config(settings)

        text = repr(settings) + str(settings) + repr(config)

        assert MAIL["MAIL_PASSWORD"] not in text

    @pytest.mark.parametrize(
        "missing", ["MAIL_USERNAME", "MAIL_PASSWORD", "MAIL_FROM", "MAIL_SERVER"]
    )
    def test_incomplete_configuration_yields_none(self, missing):
        assert mail.build_mail_config(_settings(**{missing: None})) is None

    def test_empty_password_counts_as_missing(self):
        assert mail.build_mail_config(_settings(MAIL_PASSWORD="")) is None

    def test_env_local_values_reach_the_mail_config(self, monkeypatch, tmp_path):
        """BE-19: MAIL_* in .env.local were ignored (os.getenv bypassed Settings)."""
        lines = [f"{key}={value}" for key, value in MAIL.items()]
        (tmp_path / ".env.local").write_text("\n".join(lines), encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("ENVIRONMENT", "dev")
        for key in MAIL:
            monkeypatch.delenv(key, raising=False)
        get_settings.cache_clear()
        try:
            config = mail.build_mail_config(get_settings())
        finally:
            get_settings.cache_clear()

        assert config is not None
        assert config.MAIL_SERVER == "smtp.example.com"


class TestMailer:
    def test_mailer_is_built_on_first_use_and_cached(self, monkeypatch, fresh_mailer):
        monkeypatch.setattr(mail, "get_settings", lambda: _settings())

        first = mail.get_mailer()

        assert isinstance(first, FastMail)
        assert mail.get_mailer() is first

    def test_unconfigured_mailer_is_none(self, monkeypatch, fresh_mailer):
        monkeypatch.setattr(mail, "get_settings", lambda: _settings(MAIL_SERVER=None))

        assert mail.get_mailer() is None

    def test_sending_without_configuration_is_skipped(self, monkeypatch):
        monkeypatch.setattr(mail, "get_mailer", lambda: None)

        asyncio.run(
            mail.send_templated_email(
                to="someone@example.com",
                subject="s",
                template_name="auth/verify_email.html",
                context={},
            )
        )
