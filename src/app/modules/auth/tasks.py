"""Background tasks of the auth module: the emails and the nightly cleanup.

The token task sends the verification, password-reset and email-change
mails; the notice tasks tell a user that the account was locked by failed
logins, or the old address that an email change was requested or
completed. Each is enqueued with one row id only (an email token's id, or
the user's id for the lock notice). Addresses, usernames and token values
are read and created in the worker, so none of them is stored in Redis.

The cleanup task deletes ended sessions and closed email tokens once a day.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from taskiq import TaskiqDepends

from app.core import (
    Settings,
    TaskDbSession,
    get_logger,
    get_mailer,
    get_settings,
    mail_delivery_error,
    send_templated_email,
    task,
)
from app.modules.auth.models import EmailTokenPurpose

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"

# Daily at 03:00 UTC, whatever the host's time zone: taskiq's scheduler
# matches cron fields against `datetime.now(tz=timezone.utc)` and shifts
# that only by a `cron_offset`, which this schedule does not set (taskiq
# 0.13, cli/scheduler/run.py, `run` and `is_cron_task_now`).
CLEANUP_CRON = "0 3 * * *"


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

ACCOUNT_LOCKED_SUBJECT = "Your account has been locked"
EMAIL_CHANGE_REQUESTED_SUBJECT = "A change of your email address was requested"
EMAIL_CHANGE_COMPLETED_SUBJECT = "Your email address was changed"


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


def lock_end_text(locked_until: datetime, now: datetime) -> str:
    """Format the end of a lock for the mail: `14:35 UTC`.

    The minute is rounded up, so the stated time is never before the lock
    ends. The date (`on 2026-11-18`, ISO 8601) is added when the lock ends
    on another UTC day than `now`.

    Args:
        locked_until: End of the lock, timezone-aware.
        now: The current time, timezone-aware.

    Returns:
        The end of the lock in UTC, for example `00:10 UTC on 2026-11-18`.
    """
    end = locked_until.astimezone(UTC)
    if end.second or end.microsecond:
        end = end.replace(second=0, microsecond=0) + timedelta(minutes=1)
    text = f"{end:%H:%M} UTC"
    if end.date() != now.astimezone(UTC).date():
        text += f" on {end:%Y-%m-%d}"
    return text


async def _send(
    log: structlog.stdlib.BoundLogger,
    to: str,
    subject: str,
    template_name: str,
    # Any: arbitrary Jinja2 template variables.
    context: dict[str, Any],
) -> bool:
    """Render and send one mail of this module and log the outcome.

    Returns:
        True once the mail is handed to the SMTP server, False when mail is
        not configured.

    Raises:
        MailDeliveryError: Rendering or sending failed; the transient
            subclass is retried.
    """
    try:
        sent = await send_templated_email(
            to=to,
            subject=subject,
            template_name=template_name,
            context=context,
            template_dir=TEMPLATE_DIR,
        )
    except Exception as exc:  # noqa: BLE001 - every failure leaves as MailDeliveryError
        # Only the exception type, in the log and in what taskiq logs: SMTP
        # errors can echo the recipient address.
        log.error("email.send_failed", error_type=type(exc).__name__)
        raise mail_delivery_error(exc) from None
    if sent:
        log.info("email.sent")
    else:
        log.warning("email.not_sent", reason="mail_not_configured")
    return sent


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
    from app.modules.auth import service  # noqa: PLC0415 - service imports tasks

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
    await _send(log, mail.recipient, spec.subject, spec.template_name, context)


@task(task_name="email.send_account_locked_notice", retry_on_error=True)
async def send_account_locked_notice_task(
    user_id: str, db: TaskDbSession = TaskiqDepends()
) -> None:
    """Tell a user that failed logins locked the account, and until when.

    Sends nothing when the timed lock has ended or became an admin lock
    meanwhile, when the account is inactive, or when a lock notice went out
    in the last 24 hours (`service.prepare_account_locked_notice`). A sent
    notice is recorded in the audit log. A redelivery can send the notice
    twice; it carries no token, so a duplicate is harmless.

    Args:
        user_id: The locked user's id as a string.
        db: The task's database session.

    Raises:
        MailDeliveryError: Rendering or sending failed; the transient
            subclass is retried.
    """
    from app.modules.auth import service  # noqa: PLC0415 - service imports tasks

    log = logger.bind(user_id=user_id, purpose="account_locked")
    if get_mailer() is None:
        log.warning("email.not_sent", reason="mail_not_configured")
        return

    notice = await service.prepare_account_locked_notice(db, uuid.UUID(user_id))
    if notice is None:
        return

    context = {
        "username": notice.username,
        "locked_until": lock_end_text(notice.locked_until, datetime.now(UTC)),
        "frontend_url": get_settings().FRONTEND_URL,
    }
    if await _send(
        log, notice.recipient, ACCOUNT_LOCKED_SUBJECT, "account_locked.html", context
    ):
        await service.record_account_locked_notice(db, notice)


async def _send_email_change_notice(
    db: AsyncSession, token_id: str, *, completed: bool
) -> None:
    """Send the requested or the completed notice of one email change."""
    from app.modules.auth import service  # noqa: PLC0415 - service imports tasks

    if completed:
        purpose, subject, template_name = (
            "email_change_completed",
            EMAIL_CHANGE_COMPLETED_SUBJECT,
            "email_change_completed.html",
        )
    else:
        purpose, subject, template_name = (
            "email_change_requested",
            EMAIL_CHANGE_REQUESTED_SUBJECT,
            "email_change_requested.html",
        )
    log = logger.bind(token_id=token_id, purpose=purpose)
    if get_mailer() is None:
        log.warning("email.not_sent", reason="mail_not_configured")
        return

    notice = await service.prepare_email_change_notice(
        db, uuid.UUID(token_id), completed=completed
    )
    if notice is None:
        return

    log = log.bind(user_id=str(notice.user_id))
    context = {
        "username": notice.username,
        "frontend_url": get_settings().FRONTEND_URL,
    }
    await _send(log, notice.recipient, subject, template_name, context)


@task(task_name="email.send_email_change_requested_notice", retry_on_error=True)
async def send_email_change_requested_notice_task(
    token_id: str, db: TaskDbSession = TaskiqDepends()
) -> None:
    """Tell the account's address that an email change was requested.

    The recipient is the address the account had when the change was
    requested: the current one, or, if this change was confirmed before the
    task runs, the address it replaced. Sends nothing for an inactive
    account. A duplicate from a redelivery is harmless (no token).

    Args:
        token_id: The email-change token's id as a string.
        db: The task's database session.

    Raises:
        MailDeliveryError: Rendering or sending failed; the transient
            subclass is retried.
    """
    await _send_email_change_notice(db, token_id, completed=False)


@task(task_name="email.send_email_change_completed_notice", retry_on_error=True)
async def send_email_change_completed_notice_task(
    token_id: str, db: TaskDbSession = TaskiqDepends()
) -> None:
    """Tell the old address that the account's email address was changed.

    The recipient is the token's `previous_email`, stored when the change
    was confirmed. Sends nothing for an inactive account. A duplicate from
    a redelivery is harmless (no token).

    Args:
        token_id: The redeemed email-change token's id as a string.
        db: The task's database session.

    Raises:
        MailDeliveryError: Rendering or sending failed; the transient
            subclass is retried.
    """
    await _send_email_change_notice(db, token_id, completed=True)


@task(
    task_name="auth.cleanup",
    retry_on_error=True,
    schedule=[{"cron": CLEANUP_CRON}],
)
async def cleanup_task(db: TaskDbSession = TaskiqDepends()) -> None:
    """Delete ended sessions and closed email tokens past the retention.

    Runs daily at 03:00 UTC with `AUTH_CLEANUP_RETENTION_DAYS`; see
    `service.cleanup_ended_auth_records`. Idempotent: a second run, in
    parallel or later, finds only what the first left. A run stops after
    `service.CLEANUP_MAX_BATCHES` batches per table (`capped=True` in the
    log); the next night continues. The audit log is not touched.

    Args:
        db: The task's database session.
    """
    from app.modules.auth import service  # noqa: PLC0415 - service imports tasks

    retention_days = get_settings().AUTH_CLEANUP_RETENTION_DAYS
    result = await service.cleanup_ended_auth_records(
        db, timedelta(days=retention_days)
    )
    logger.info(
        "auth.cleanup_done",
        sessions_deleted=result.sessions_deleted,
        email_tokens_deleted=result.email_tokens_deleted,
        capped=result.capped,
        retention_days=retention_days,
    )
