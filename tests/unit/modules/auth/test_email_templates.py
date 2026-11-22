"""The auth email templates render with the core layout."""

import pytest

from app.core.mail import render_email
from app.modules.auth.tasks import TEMPLATE_DIR


@pytest.mark.parametrize(
    ("template_name", "url_key"),
    [
        ("verify_email.html", "verify_url"),
        ("reset_password.html", "reset_url"),
        ("confirm_email_change.html", "confirm_url"),
    ],
)
def test_auth_templates_extend_the_core_layout(
    template_name: str, url_key: str
) -> None:
    html = render_email(
        template_name,
        {
            "app_name": "Probe App",
            "username": "<script>x</script>",
            "token": "tok-123",
            url_key: "https://example.com/link?token=tok-123",
        },
        TEMPLATE_DIR,
    )

    assert "<html" in html.lower()  # from core base.html
    assert "tok-123" in html
    assert "<script>x</script>" not in html  # autoescaped
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize(
    "template_name",
    [
        "account_locked.html",
        "email_change_requested.html",
        "email_change_completed.html",
    ],
)
def test_notice_templates_extend_the_layout_and_escape(template_name: str) -> None:
    html = render_email(
        template_name,
        {
            "app_name": "Probe App",
            "username": "<script>x</script>",
            "locked_until": "14:35 UTC",
            "frontend_url": "https://app.example.com",
        },
        TEMPLATE_DIR,
    )

    assert "<html" in html.lower()
    assert 'href="https://app.example.com"' in html
    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html
    assert "If this wasn't you" in html or "If this wasn&#39;t you" in html


def test_lock_notice_states_the_end_in_utc() -> None:
    html = render_email(
        "account_locked.html",
        {"username": "a", "locked_until": "00:10 UTC on 2026-11-18"},
        TEMPLATE_DIR,
    )

    assert "00:10 UTC on 2026-11-18" in html


def test_completed_notice_says_how_to_get_the_account_back() -> None:
    """A new password alone is not enough: the new address can reset it again."""
    html = render_email(
        "email_change_completed.html",
        {
            "app_name": "Probe App",
            "username": "a",
            "frontend_url": "https://app.example.com",
        },
        TEMPLATE_DIR,
    )

    assert "Change your password." in html
    assert "Change the account's email address back to this one" in html
    assert "sent to this address" in html
    assert "contact the support of Probe App from this address" in html
