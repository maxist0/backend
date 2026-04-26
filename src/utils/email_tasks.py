# src/utils/email_tasks.py

"""
Celery tasks for sending transactional emails.

Each task:
  - accepts plain serialisable arguments (str, dict)
  - renders the appropriate Jinja2 template via send_templated_email
  - retries up to 3 times on failure (60 s back-off)

All tasks are discovered automatically because this module is listed in
celery.conf include (see src/common/celery.py).
"""

import asyncio

from src.common.celery import celery
from src.common.config import settings
from src.common.logging import get_logger
from src.common.mail import send_templated_email

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    """Run a coroutine synchronously inside a Celery worker."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Auth emails
# ---------------------------------------------------------------------------


@celery.task(
    name="email.send_verification_email",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def send_verification_email_task(self, to: str, username: str, token: str) -> None:
    """Dispatch an email-verification message to *to*."""
    verify_url = f"{settings.FRONTEND_URL}/auth/verify-email?token={token}"
    context = {
        "username": username,
        "token": token,
        "verify_url": verify_url,
    }
    try:
        _run(
            send_templated_email(
                to=to,
                subject="Verify your email address",
                template_name="auth/verify_email.html",
                context=context,
            )
        )
        logger.info("email.verification_sent", to=to, username=username)
    except Exception as exc:
        logger.error("email.verification_failed", to=to, error=str(exc))
        raise self.retry(exc=exc)


@celery.task(
    name="email.send_password_reset_email",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    acks_late=True,
)
def send_password_reset_email_task(self, to: str, username: str, token: str) -> None:
    """Dispatch a password-reset message to *to*."""
    reset_url = f"/auth/reset-password?token={token}"
    context = {
        "username": username,
        "token": token,
        "reset_url": reset_url,
    }
    try:
        _run(
            send_templated_email(
                to=to,
                subject="Reset your password",
                template_name="auth/reset_password.html",
                context=context,
            )
        )
        logger.info("email.password_reset_sent", to=to, username=username)
    except Exception as exc:
        logger.error("email.password_reset_failed", to=to, error=str(exc))
        raise self.retry(exc=exc)