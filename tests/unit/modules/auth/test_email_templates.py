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
def test_auth_templates_extend_the_core_layout(template_name, url_key):
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
