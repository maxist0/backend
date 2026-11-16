# src/app/common/mail.py

"""
Transactional email via fastapi-mail.

The mail client is built lazily on the first send (in practice: in the taskiq
worker) from ``Settings``; nothing is configured at import time. When the
``MAIL_*`` settings are incomplete, sending is skipped with a warning.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi_mail import (
    ConnectionConfig,
    FastMail,
    MessageSchema,
    MessageType,
    NameEmail,
)

from app.common.config import Settings, get_settings
from app.common.logging import get_logger

logger = get_logger(__name__)

TEMPLATE_FOLDER = Path(__file__).resolve().parent.parent / "templates" / "email"


def build_mail_config(settings: Settings) -> ConnectionConfig | None:
    """Build the fastapi-mail configuration from *settings*.

    Returns None when username, password, sender or server is missing.
    """
    password = settings.MAIL_PASSWORD
    if not (
        settings.MAIL_USERNAME
        and password is not None
        and password.get_secret_value()
        and settings.MAIL_FROM
        and settings.MAIL_SERVER
    ):
        return None
    return ConnectionConfig(
        MAIL_USERNAME=settings.MAIL_USERNAME,
        MAIL_PASSWORD=password,
        MAIL_FROM=settings.MAIL_FROM,
        MAIL_PORT=settings.MAIL_PORT,
        MAIL_SERVER=settings.MAIL_SERVER,
        MAIL_STARTTLS=settings.MAIL_STARTTLS,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=True,
        TEMPLATE_FOLDER=TEMPLATE_FOLDER,
    )


@lru_cache(maxsize=1)
def get_mailer() -> FastMail | None:
    """Return the process-wide mail client, built on first use (or None)."""
    config = build_mail_config(get_settings())
    if config is None:
        logger.warning("mail.not_configured")
        return None
    # Server and port only: the username is often an email address (PII).
    logger.info(
        "mail.configured",
        server=config.MAIL_SERVER,
        port=config.MAIL_PORT,
        starttls=config.MAIL_STARTTLS,
    )
    return FastMail(config)


async def send_templated_email(
    to: str,
    subject: str,
    template_name: str,
    # Any: arbitrary Jinja2 template variables.
    context: dict[str, Any],
) -> None:
    """Render *template_name* with *context* via fastapi-mail and send it."""
    mailer = get_mailer()
    if mailer is None:
        logger.warning("mail.skipped_not_configured", template=template_name)
        return
    context.setdefault("app_name", get_settings().APP_NAME)
    recipient = NameEmail(name=context.get("username", ""), email=to)
    message = MessageSchema(
        subject=subject,
        recipients=[recipient],
        template_body=context,
        subtype=MessageType.html,
    )
    await mailer.send_message(message, template_name=template_name)
