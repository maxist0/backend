"""Background tasks of the auth module: transactional emails."""

from pathlib import Path

from app.core import get_logger, get_settings, task
from app.core.mail import send_templated_email

logger = get_logger(__name__)

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"


@task(
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
            template_name="verify_email.html",
            context=context,
            template_dir=TEMPLATE_DIR,
        )
        logger.info("email.verification_sent")
    except Exception as exc:
        # Only the exception type: SMTP errors can echo the recipient address.
        logger.error("email.verification_failed", error_type=type(exc).__name__)
        raise


@task(
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
            template_name="reset_password.html",
            context=context,
            template_dir=TEMPLATE_DIR,
        )
        logger.info("email.password_reset_sent")
    except Exception as exc:
        # Only the exception type: SMTP errors can echo the recipient address.
        logger.error("email.password_reset_failed", error_type=type(exc).__name__)
        raise
