# src/utils/email_tasks.py

"""Taskiq tasks for transactional emails."""

from src.common.config import settings
from src.common.logging import get_logger
from src.common.mail import send_templated_email
from src.common.taskiq import broker

logger = get_logger(__name__)


@broker.task(
    task_name="email.send_verification_email",
    retry_on_error=True,
)
async def send_verification_email_task(to: str, username: str, token: str) -> None:
    """Dispatch an email-verification message to *to*."""
    verify_url = f"{settings.FRONTEND_URL}/auth/verify-email?token={token}"
    context = {
        "username": username,
        "token": token,
        "verify_url": verify_url,
    }
    try:
        await send_templated_email(
            to=to,
            subject="Verify your email address",
            template_name="auth/verify_email.html",
            context=context,
        )
        logger.info("email.verification_sent", to=to, username=username)
    except Exception as exc:
        logger.error("email.verification_failed", to=to, error=str(exc))
        raise


@broker.task(
    task_name="email.send_password_reset_email",
    retry_on_error=True,
)
async def send_password_reset_email_task(to: str, username: str, token: str) -> None:
    """Dispatch a password-reset message to *to*."""
    reset_url = f"/auth/reset-password?token={token}"
    context = {
        "username": username,
        "token": token,
        "reset_url": reset_url,
    }
    try:
        await send_templated_email(
            to=to,
            subject="Reset your password",
            template_name="auth/reset_password.html",
            context=context,
        )
        logger.info("email.password_reset_sent", to=to, username=username)
    except Exception as exc:
        logger.error("email.password_reset_failed", to=to, error=str(exc))
        raise
