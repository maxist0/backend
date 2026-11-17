"""Email links: absolute frontend URLs with the token URL-encoded."""

import pytest
from pydantic import ValidationError

from app.modules.auth.models import EmailTokenPurpose
from app.modules.auth.tasks import token_link


class TestTokenLink:
    @pytest.mark.parametrize(
        ("purpose", "path"),
        [
            (EmailTokenPurpose.VERIFY_EMAIL, "/user/email-bestaetigen"),
            (EmailTokenPurpose.CHANGE_EMAIL, "/user/email-bestaetigen"),
            (EmailTokenPurpose.RESET_PASSWORD, "/user/passwort-zuruecksetzen"),
        ],
    )
    def test_default_links_open_the_frontend_pages(
        self, settings_factory, purpose, path
    ):
        link = token_link(settings_factory(), purpose, "abc_-123")

        assert link == f"http://localhost:5173{path}?token=abc_-123"

    def test_token_is_url_encoded(self, settings_factory):
        link = token_link(
            settings_factory(), EmailTokenPurpose.RESET_PASSWORD, "a+b/c=d&e f"
        )

        assert link.endswith("?token=a%2Bb%2Fc%3Dd%26e+f")

    def test_base_url_and_paths_come_from_the_settings(self, settings_factory):
        settings = settings_factory(
            FRONTEND_URL="https://app.example.com/",
            FRONTEND_VERIFY_EMAIL_PATH="/en/user/verify-email",
            FRONTEND_RESET_PASSWORD_PATH="/en/user/reset-password",
        )

        assert token_link(settings, EmailTokenPurpose.VERIFY_EMAIL, "t") == (
            "https://app.example.com/en/user/verify-email?token=t"
        )
        assert token_link(settings, EmailTokenPurpose.RESET_PASSWORD, "t") == (
            "https://app.example.com/en/user/reset-password?token=t"
        )


class TestFrontendPathSettings:
    @pytest.mark.parametrize(
        "name", ["FRONTEND_VERIFY_EMAIL_PATH", "FRONTEND_RESET_PASSWORD_PATH"]
    )
    @pytest.mark.parametrize(
        "value",
        [
            "https://evil.example/x",  # scheme and host
            "//evil.example/x",  # host without scheme
            "user/x",  # relative
            "",
            "/",
            "/a//b",
            "/x?token=1",  # the link adds the query
            "/x#y",
            "/x y",
            "/x\\y",
        ],
    )
    def test_only_absolute_paths_are_accepted(self, settings_factory, name, value):
        with pytest.raises(ValidationError):
            settings_factory(**{name: value})

    def test_frontend_url_default_is_the_dev_server(self, settings_factory):
        assert settings_factory().FRONTEND_URL == "http://localhost:5173"


class TestFrontendUrlSetting:
    @pytest.mark.parametrize(
        "value",
        [
            "",  # relative links
            "javascript:alert(1)//",
            "ftp://app.example.com",
            "https:/",
            "https://",
            "https://app.example.com?x=",  # the link appends ?token=
            "https://app.example.com#",
            "https://user@evil.example",  # userinfo disguises the host
            "https://app.example.com/a b",
            "https://app.example.com\\evil",
            "https://app.example.com/\x7f",
            "https://app.example.com\n",
        ],
    )
    def test_bad_values_are_refused(self, settings_factory, value):
        with pytest.raises(ValidationError):
            settings_factory(FRONTEND_URL=value)

    @pytest.mark.parametrize(
        "value",
        [
            "https://app.example.com",
            "https://app.example.com:8443/base/",
            "http://localhost:5173",
            "https://[::1]:8443",
        ],
    )
    def test_origins_with_an_optional_path_are_accepted(self, settings_factory, value):
        assert settings_factory(FRONTEND_URL=value).FRONTEND_URL == value
