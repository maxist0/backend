"""Transactional email via fastapi-mail.

The mail client is built lazily on the first send (in practice: in the taskiq
worker) from `Settings`; nothing is configured at import time. When the
`MAIL_*` settings are incomplete, `get_mailer` returns None and
`send_templated_email` sends nothing and returns False.

Templates are rendered here, not by fastapi-mail: fastapi-mail accepts a
single `TEMPLATE_FOLDER`, while a module's templates live in the module and
extend `base.html` from the core. The search path is the module's directory
first, then `CORE_TEMPLATE_DIR`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from aiosmtplib import SMTPRecipientsRefused, SMTPResponseException
from fastapi_mail import (
    ConnectionConfig,
    FastMail,
    MessageSchema,
    MessageType,
    NameEmail,
)
from fastapi_mail.errors import ConnectionErrors
from jinja2 import Environment, FileSystemLoader

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.core.tasks import TransientTaskError

logger = get_logger(__name__)

CORE_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


class MailDeliveryError(RuntimeError):
    """Sending a mail failed for good; carries only the type of the cause.

    Mail tasks raise it `from None` instead of the original exception: SMTP
    errors can echo the recipient address, and taskiq logs the exception of
    a failed task with its message and traceback. Build it with
    `mail_delivery_error`, which picks the retried subclass for transient
    causes.
    """

    def __init__(self, cause: BaseException | str) -> None:
        # A string is the type name itself: taskiq rebuilds a stored error
        # as `cls(*args)` when it loads a task result.
        super().__init__(cause if isinstance(cause, str) else type(cause).__name__)


class TransientMailDeliveryError(MailDeliveryError, TransientTaskError):
    """Sending a mail failed for a reason that may pass; the task is retried."""


def _is_transient(cause: BaseException) -> bool:
    """Tell whether a failed send may succeed when tried again.

    Transient: network errors and timeouts, and SMTP replies in the 4xx
    range ("transient negative completion", RFC 5321 section 4.2.1). A 5xx
    reply (unknown mailbox, bad credentials) and every other exception
    (template errors, programming errors) are permanent.
    """
    # fastapi-mail turns every error of connect and login into its own
    # ConnectionErrors, raised inside the `except` block, so the aiosmtplib
    # error is the implicit context (fastapi_mail/connection.py).
    if isinstance(cause, ConnectionErrors) and cause.__context__ is not None:
        cause = cause.__context__
    if isinstance(cause, SMTPResponseException):
        return 400 <= cause.code < 500
    if isinstance(cause, SMTPRecipientsRefused):
        return all(400 <= refused.code < 500 for refused in cause.recipients)
    # aiosmtplib raises connection failures and disconnects as
    # ConnectionError subclasses and timeouts as TimeoutError subclasses.
    return isinstance(cause, (ConnectionError, TimeoutError))


def mail_delivery_error(cause: BaseException) -> MailDeliveryError:
    """Wrap the cause of a failed send for raising `from None`.

    Args:
        cause: The exception raised while rendering or sending.

    Returns:
        `TransientMailDeliveryError` (retried) for a transient cause,
        otherwise `MailDeliveryError`. The message is only the cause's type.
    """
    if _is_transient(cause):
        return TransientMailDeliveryError(cause)
    return MailDeliveryError(cause)


def build_mail_config(settings: Settings) -> ConnectionConfig | None:
    """Build the fastapi-mail configuration from `settings`.

    Args:
        settings: Process settings with the `MAIL_*` values.

    Returns:
        The connection configuration, or None when username, password,
        sender or server is missing.
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


# One environment per template directory; the number of modules bounds it.
@lru_cache(maxsize=32)
def _template_environment(template_dir: Path | None) -> Environment:
    search_path = [CORE_TEMPLATE_DIR]
    if template_dir is not None:
        search_path.insert(0, template_dir)
    # autoescape=True as in fastapi-mail: context values (usernames) are
    # user input.
    return Environment(loader=FileSystemLoader(search_path), autoescape=True)


def render_email(
    template_name: str,
    # Any: arbitrary Jinja2 template variables.
    context: dict[str, Any],
    template_dir: Path | None = None,
) -> str:
    """Render an HTML email template.

    Args:
        template_name: Template path relative to the search path, for
            example `verify_email.html`.
        context: Template variables.
        template_dir: The calling module's email template directory. It is
            searched before `CORE_TEMPLATE_DIR`, so its templates can extend
            `base.html`.

    Returns:
        The rendered HTML.
    """
    template = _template_environment(template_dir).get_template(template_name)
    return template.render(**context)


async def send_templated_email(
    to: str,
    subject: str,
    template_name: str,
    # Any: arbitrary Jinja2 template variables.
    context: dict[str, Any],
    template_dir: Path | None = None,
) -> bool:
    """Render a template and send it as an HTML email.

    Args:
        to: Recipient address.
        subject: Subject line.
        template_name: Template path, see `render_email`.
        context: Template variables; `app_name` defaults to `APP_NAME`.
        template_dir: The calling module's email template directory.

    Returns:
        True once the mail is handed to the SMTP server, False when mail is
        not configured (`get_mailer` is None) and nothing was sent. The
        caller logs the outcome; this function logs nothing per mail.
    """
    mailer = get_mailer()
    if mailer is None:
        return False
    context.setdefault("app_name", get_settings().APP_NAME)
    html = render_email(template_name, context, template_dir)
    recipient = NameEmail(name=context.get("username", ""), email=to)
    message = MessageSchema(
        subject=subject,
        recipients=[recipient],
        body=html,
        subtype=MessageType.html,
    )
    await mailer.send_message(message)
    return True
