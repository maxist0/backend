# src/app/utils/email_tasks.py

"""Taskiq tasks for transactional emails."""

from app.common.config import get_settings
from app.common.logging import get_logger
from app.common.mail import send_templated_email
from app.common.taskiq import broker

logger = get_logger(__name__)


@broker.task(
    task_name="email.send_verification_email",
    retry_on_error=True,
)
async def send_verification_email_task(to: str, username: str, token: str) -> None:
    """Dispatch an email-verification message to *to*."""
    verify_url = f"{get_settings().FRONTEND_URL}/auth/verify-email?token={token}"
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
        logger.info("email.verification_sent")
    except Exception as exc:
        # Only the exception type: SMTP errors can echo the recipient address.
        logger.error("email.verification_failed", error_type=type(exc).__name__)
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
        logger.info("email.password_reset_sent")
    except Exception as exc:
        # Only the exception type: SMTP errors can echo the recipient address.
        logger.error("email.password_reset_failed", error_type=type(exc).__name__)
        raise
