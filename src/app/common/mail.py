from fastapi_mail import (
    ConnectionConfig,
    FastMail,
    MessageSchema,
    MessageType,
    NameEmail,
)
from app.common.config import settings
from app.common.logging import get_logger
from typing import Optional
from pathlib import Path
import os

logger = get_logger(__name__)


def setup_mail() -> Optional[FastMail]:
    # Check if mail settings are configured; if not, skip mail setup (e.g. for local dev without .env)
    if not all(
        [
            os.getenv("MAIL_USERNAME"),
            os.getenv("MAIL_PASSWORD"),
            os.getenv("MAIL_FROM"),
            os.getenv("MAIL_SERVER"),
        ]
    ):
        logger.warning("Mail settings not fully configured; skipping mail setup.")
        return None
    template_folder = Path(__file__).parent.parent / "templates" / "email"
    mail_config = ConnectionConfig(
        MAIL_USERNAME=os.getenv("MAIL_USERNAME"),  # type: ignore[arg-type]
        MAIL_PASSWORD=os.getenv("MAIL_PASSWORD"),  # type: ignore[arg-type]
        MAIL_FROM=os.getenv("MAIL_FROM"),  # type: ignore[arg-type]
        MAIL_PORT=int(os.getenv("MAIL_PORT", 587)),
        MAIL_SERVER=os.getenv("MAIL_SERVER"),  # type: ignore[arg-type]
        MAIL_STARTTLS=True,
        MAIL_SSL_TLS=False,
        USE_CREDENTIALS=True,
        TEMPLATE_FOLDER=template_folder,
    )
    logger.info(
        "Mail configuration set up successfully.",
        server=os.getenv("MAIL_SERVER"),
        port=os.getenv("MAIL_PORT", 587),
        starttls=True,
        ssl_tls=mail_config.MAIL_SSL_TLS,
        use_credentials=mail_config.USE_CREDENTIALS,
        template_folder=template_folder,
    )
    fm = FastMail(mail_config)
    return fm


# Module-level singleton – imported by email_tasks and other consumers
fm: Optional[FastMail] = setup_mail()


async def send_templated_email(
    to: str, subject: str, template_name: str, context: dict
) -> None:
    """Render *template_name* with *context* via fastapi-mail's Jinja2 integration and send it."""
    if fm is None:
        logger.warning("FastMail not configured; skipping email.", subject=subject)
        return
    context.setdefault("app_name", settings.APP_NAME)
    _to = NameEmail(name=context.get("username", ""), email=to)
    message = MessageSchema(
        subject=subject,
        recipients=[_to],
        template_body=context,
        subtype=MessageType.html,
    )
    await fm.send_message(message, template_name=template_name)
