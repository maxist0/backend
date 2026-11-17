"""Background tasks of the auth module: the token emails.

One task sends the verification, password-reset and email-change mails. It
is enqueued with the email token's id only; the token value, the address and
the username are read and created in the worker
(`service.prepare_token_email`), so none of them is stored in Redis.
"""

import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

from taskiq import TaskiqDepends

from app.core import Settings, TaskDbSession, get_logger, get_settings, task
from app.core.mail import get_mailer, mail_delivery_error, send_templated_email
from app.modules.auth.models import EmailTokenPurpose

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@dataclass(frozen=True)
class _TokenMail:
    subject: str
    template_name: str
    # Template variable that holds the link.
    url_variable: str


_TOKEN_MAILS = {
    EmailTokenPurpose.VERIFY_EMAIL: _TokenMail(
        "Verify your email address", "verify_email.html", "verify_url"
    ),
    EmailTokenPurpose.RESET_PASSWORD: _TokenMail(
        "Reset your password", "reset_password.html", "reset_url"
    ),
    EmailTokenPurpose.CHANGE_EMAIL: _TokenMail(
        "Confirm your new email address", "confirm_email_change.html", "confirm_url"
    ),
}


def token_link(settings: Settings, purpose: EmailTokenPurpose, token: str) -> str:
    """Build the absolute frontend link that carries an email token.

    Verification and email-change links open the same page: it redeems both
    token types with `POST /auth/email/verify`.

    Args:
        settings: Settings with `FRONTEND_URL` and the frontend paths.
        purpose: The token's purpose; it selects the page.
        token: The raw token; URL-encoded in the query string.

    Returns:
        `{FRONTEND_URL}{path}?token=<token>`.
    """
    path = (
        settings.FRONTEND_RESET_PASSWORD_PATH
        if purpose is EmailTokenPurpose.RESET_PASSWORD
        else settings.FRONTEND_VERIFY_EMAIL_PATH
    )
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}?{urlencode({'token': token})}"


@task(task_name="email.send_token_email", retry_on_error=True)
async def send_token_email_task(
    token_id: str, db: TaskDbSession = TaskiqDepends()
) -> None:
    """Mail a fresh link for one open email token.

    Idempotent and retry-safe: each run gives the token a new value and
    mails it, so only the latest mail's link works. A run that finds the
    token no longer open (expired, redeemed or cancelled) sends nothing.
    Without a mail configuration nothing is sent and the token is not
    touched.

    Args:
        token_id: The email token's id as a string.
        db: The task's database session.

    Raises:
        MailDeliveryError: Rendering or sending failed; the transient
            subclass is retried.
    """
    # The service enqueues this task, so it is imported when the task runs.
    from app.modules.auth import service

    log = logger.bind(token_id=token_id)
    if get_mailer() is None:
        log.warning("email.not_sent", reason="mail_not_configured")
        return

    mail = await service.prepare_token_email(db, uuid.UUID(token_id))
    if mail is None:
        return

    log = log.bind(user_id=str(mail.user_id), purpose=mail.purpose.value)
    spec = _TOKEN_MAILS[mail.purpose]
    context = {
        "username": mail.username,
        "token": mail.token,
        spec.url_variable: token_link(get_settings(), mail.purpose, mail.token),
    }
    try:
        sent = await send_templated_email(
            to=mail.recipient,
            subject=spec.subject,
            template_name=spec.template_name,
            context=context,
            template_dir=TEMPLATE_DIR,
        )
    except Exception as exc:
        # Only the exception type, in the log and in what taskiq logs: SMTP
        # errors can echo the recipient address.
        log.error("email.send_failed", error_type=type(exc).__name__)
        raise mail_delivery_error(exc) from None
    if sent:
        log.info("email.sent")
    else:
        log.warning("email.not_sent", reason="mail_not_configured")
