# src/api/auth/service.py

"""
Business logic for authentication.
Orchestrates CRUD operations, password hashing, token creation, and audit logging.
"""

import secrets
import uuid
from datetime import timedelta
from typing import Optional, Tuple

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
import redis.asyncio as aioredis # type: ignore

from src.api.auth import crud
from src.api.auth.jwt_utils import create_access_token
from src.api.auth.models import (
    AuditEventType,
    AuthAuditLog,
    EmailToken,
    EmailTokenPurpose,
    PasswordCredential,
    RefreshTokenCache,
    Session,
    User,
    UserResponse,
    hash_token,
    utcnow,
)
from src.common.config import settings
from src.common.constants import MAXIMUM_LOGIN_ATTEMPTS, LOGIN_ATTEMPT_WINDOW_SECONDS
from src.common.logging import get_logger

logger = get_logger(__name__)

_ph = PasswordHasher()


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Return an argon2id hash of *password*."""
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Return True if *password* matches *password_hash*."""
    try:
        return _ph.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# ---------------------------------------------------------------------------
# User → response helper
# ---------------------------------------------------------------------------


def user_to_response(user: User) -> UserResponse:
    """Convert a User ORM object to a UserResponse Pydantic model."""
    return UserResponse(
        id=str(user.id),
        username=user.username,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------


def add_refresh_token_to_cookie(
    response: JSONResponse, refresh_token: str
) -> JSONResponse:
    """Set the refresh token as an httpOnly cookie."""
    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        samesite="lax",
        path="/auth",
    )
    return response


def delete_refresh_token_cookie(response: JSONResponse) -> JSONResponse:
    """Clear the refresh token cookie."""
    response.delete_cookie(key="refresh_token", path="/auth")
    return response


# ---------------------------------------------------------------------------
# Audit helper
# ---------------------------------------------------------------------------


async def _audit(
    db: AsyncSession,
    event: AuditEventType,
    user_id: Optional[uuid.UUID],
    request: Optional[Request] = None,
    detail: Optional[str] = None,
) -> None:
    """Create an immutable audit-log entry."""
    log = AuthAuditLog(
        user_id=user_id,
        event_type=event,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
        detail=detail,
    )
    await crud.create_audit_log(db, log)


# ---------------------------------------------------------------------------
# Email stub
# ---------------------------------------------------------------------------


async def _send_email_stub(to: str, subject: str, token: str) -> None:
    """Placeholder – log instead of sending real email."""
    logger.info("auth.email_stub_sent", to=to, subject=subject, token=token)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def register_user(
    db: AsyncSession,
    username: str,
    email: str,
    password: str,
    display_name: Optional[str] = None,
    request: Optional[Request] = None,
) -> Tuple[Optional[User], Optional[str]]:
    """
    Register a new user.
    Returns (User, None) on success or (None, error_message) on failure.
    """
    # Check uniqueness
    if await crud.get_user_by_username(db, username):
        return None, "Username already taken"
    if await crud.get_user_by_email(db, email):
        return None, "Email already registered"

    # Create user
    user = User(
        username=username,
        email=email,
        display_name=display_name,
        is_email_verified=False,
    )
    await crud.create_user(db, user)

    # Create password credential
    credential = PasswordCredential(
        user_id=user.id,
        password_hash=hash_password(password),
    )
    await crud.create_password_credential(db, credential)

    # Create email verification token
    raw_token = secrets.token_urlsafe(32)
    email_token = EmailToken(
        user_id=user.id,
        purpose=EmailTokenPurpose.VERIFY_EMAIL,
        token_hash=hash_token(raw_token),
        expires_at=utcnow() + timedelta(hours=24),
    )
    await crud.create_email_token(db, email_token)

    # Audit
    await _audit(db, AuditEventType.REGISTER, user.id, request)
    await _audit(db, AuditEventType.EMAIL_VERIFICATION_SENT, user.id, request)

    # Stub email
    await _send_email_stub(email, "Verify your email", raw_token)

    logger.info("auth.user_registered", username=username, user_id=str(user.id))
    return user, None


# ---------------------------------------------------------------------------
# Authentication (login)
# ---------------------------------------------------------------------------


async def authenticate_user(
    db: AsyncSession,
    username: str,
    password: str,
    request: Optional[Request] = None,
) -> Tuple[Optional[User], Optional[str]]:
    """
    Verify credentials. Handles account locking on repeated failures.
    Returns (User, None) on success or (None, error_message) on failure.
    """
    user = await crud.get_user_by_username(db, username)
    if user is None:
        return None, "Invalid username or password"

    # Check if account is locked
    if user.is_locked:
        if user.locked_until and user.locked_until > utcnow():
            remaining = int((user.locked_until - utcnow()).total_seconds())
            await _audit(
                db,
                AuditEventType.LOGIN_FAILED,
                user.id,
                request,
                f"Account locked, {remaining}s remaining",
            )
            return None, f"Account is locked. Try again in {remaining} seconds"
        else:
            # Lock expired → unlock
            user.is_locked = False
            user.failed_login_attempts = 0
            user.locked_until = None
            await crud.update_user(db, user)
            await _audit(
                db,
                AuditEventType.ACCOUNT_UNLOCKED,
                user.id,
                request,
                "Lock expired",
            )

    if not user.is_active:
        return None, "Account is deactivated"

    # Verify password
    credential = await crud.get_password_credential(db, user.id)
    if credential is None or not verify_password(password, credential.password_hash):
        # Increment failed attempts
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAXIMUM_LOGIN_ATTEMPTS:
            user.is_locked = True
            user.locked_until = utcnow() + timedelta(
                seconds=LOGIN_ATTEMPT_WINDOW_SECONDS
            )
            await crud.update_user(db, user)
            await _audit(
                db,
                AuditEventType.ACCOUNT_LOCKED,
                user.id,
                request,
                f"Locked after {user.failed_login_attempts} failed attempts",
            )
            return None, "Account has been locked due to too many failed login attempts"
        await crud.update_user(db, user)
        await _audit(db, AuditEventType.LOGIN_FAILED, user.id, request)
        return None, "Invalid username or password"

    # Success → reset counters
    user.failed_login_attempts = 0
    user.last_login_at = utcnow()
    await crud.update_user(db, user)
    await _audit(db, AuditEventType.LOGIN_SUCCESS, user.id, request)

    return user, None


# ---------------------------------------------------------------------------
# Session / Refresh-token management
# ---------------------------------------------------------------------------


async def create_session_tokens(
    db: AsyncSession,
    user: User,
    redis_client: aioredis.Redis,
    request: Optional[Request] = None,
    family_id: Optional[uuid.UUID] = None,
) -> Tuple[str, str]:
    """
    Create an access token + refresh token (with DB session and Redis cache).
    Returns (access_token, raw_refresh_token).
    """
    access_token = create_access_token(user.username, str(user.id))

    # Generate opaque refresh token
    raw_refresh = secrets.token_urlsafe(32)
    token_hash_val = hash_token(raw_refresh)
    fam_id = family_id or uuid.uuid4()
    expires = utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    session = Session(
        user_id=user.id,
        token_hash=token_hash_val,
        family_id=fam_id,
        expires_at=expires,
        device_id=request.headers.get("x-device-id") if request else None,
        device_name=request.headers.get("x-device-name") if request else None,
        ip_address=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None,
    )
    await crud.create_session(db, session)

    # Cache in Redis
    cache_data = RefreshTokenCache(
        session_id=str(session.id),
        user_id=str(user.id),
        username=user.username,
        family_id=str(fam_id),
        expires_at=expires,
    )
    await crud.cache_refresh_token(raw_refresh, cache_data, redis_client)

    return access_token, raw_refresh


async def refresh_session(
    db: AsyncSession,
    raw_token: str,
    redis_client: aioredis.Redis,
    request: Optional[Request] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Rotate a refresh token. Detects token reuse.
    Returns (access_token, raw_refresh_token, None) on success,
    or (None, None, error_message) on failure.
    """
    token_hash_val = hash_token(raw_token)

    # 1. Try Redis first (fast path)
    cached = await crud.get_cached_refresh_token(raw_token, redis_client)
    if cached:
        # Token is valid in cache → rotate
        await crud.delete_cached_refresh_token(raw_token, redis_client)

        # Revoke old session in DB
        sess = await crud.get_session_by_token_hash(db, token_hash_val)
        if sess:
            await crud.revoke_session(db, sess.id, reason="rotated")
            family_id = sess.family_id
        else:
            family_id = uuid.UUID(cached.family_id)

        # Create new session
        user = await crud.get_user_by_id(db, uuid.UUID(cached.user_id))
        if user is None:
            return None, None, "User not found"

        access_token, new_raw_refresh = await create_session_tokens(
            db, user, redis_client, request, family_id=family_id
        )
        return access_token, new_raw_refresh, None

    # 2. Not in Redis → check DB for reuse detection
    sess = await crud.get_session_by_token_hash(db, token_hash_val)
    if sess and sess.is_revoked:
        # Token reuse detected! Revoke the entire family.
        logger.warning(
            "auth.token_reuse_detected",
            family_id=str(sess.family_id),
            user_id=str(sess.user_id),
        )
        revoked_sessions = await crud.revoke_family(
            db, sess.family_id, reason="reuse_detected"
        )
        await _audit(
            db,
            AuditEventType.SESSION_REVOKED,
            sess.user_id,
            request,
            f"Token reuse detected, revoked {len(revoked_sessions)} session(s) "
            f"in family {sess.family_id}",
        )
        return None, None, "Token reuse detected – all sessions revoked"

    return None, None, "Invalid or expired refresh token"


async def logout_session(
    db: AsyncSession,
    raw_token: str,
    redis_client: aioredis.Redis,
    request: Optional[Request] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Revoke the refresh token (logout).
    Returns (True, None) on success or (False, error_message) on failure.
    """
    # Remove from Redis
    await crud.delete_cached_refresh_token(raw_token, redis_client)

    # Mark revoked in DB
    token_hash_val = hash_token(raw_token)
    sess = await crud.get_session_by_token_hash(db, token_hash_val)
    if sess:
        await crud.revoke_session(db, sess.id, reason="logout")
        await _audit(
            db, AuditEventType.SESSION_REVOKED, sess.user_id, request, "Logout"
        )
        return True, None

    return False, "Session not found"


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


async def forgot_password(
    db: AsyncSession,
    email: str,
    request: Optional[Request] = None,
) -> None:
    """
    Initiate a password reset.
    Always returns successfully to avoid email enumeration.
    """
    user = await crud.get_user_by_email(db, email.lower())
    if user is None:
        # Don't reveal whether the email exists
        return

    # Invalidate old reset tokens
    await crud.invalidate_tokens_for_user_purpose(
        db, user.id, EmailTokenPurpose.RESET_PASSWORD
    )

    raw_token = secrets.token_urlsafe(32)
    email_token = EmailToken(
        user_id=user.id,
        purpose=EmailTokenPurpose.RESET_PASSWORD,
        token_hash=hash_token(raw_token),
        expires_at=utcnow() + timedelta(hours=1),
    )
    await crud.create_email_token(db, email_token)
    await _audit(db, AuditEventType.PASSWORD_RESET_REQUESTED, user.id, request)
    await _send_email_stub(email, "Reset your password", raw_token)


async def reset_password(
    db: AsyncSession,
    raw_token: str,
    new_password: str,
    request: Optional[Request] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Complete the password-reset flow.
    Returns (True, None) on success or (False, error_message) on failure.
    """
    token_hash_val = hash_token(raw_token)
    email_token = await crud.get_valid_email_token(
        db, token_hash_val, EmailTokenPurpose.RESET_PASSWORD
    )
    if email_token is None:
        return False, "Invalid or expired reset token"

    await crud.mark_email_token_used(db, email_token.id)
    await crud.update_password_hash(
        db, email_token.user_id, hash_password(new_password)
    )
    await _audit(
        db, AuditEventType.PASSWORD_RESET_COMPLETED, email_token.user_id, request
    )
    logger.info("auth.password_reset_completed", user_id=str(email_token.user_id))
    return True, None


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


async def verify_email(
    db: AsyncSession,
    raw_token: str,
    request: Optional[Request] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Verify the user's email address.
    Returns (True, None) on success or (False, error_message).
    """
    token_hash_val = hash_token(raw_token)
    email_token = await crud.get_valid_email_token(
        db, token_hash_val, EmailTokenPurpose.VERIFY_EMAIL
    )
    if email_token is None:
        return False, "Invalid or expired verification token"

    await crud.mark_email_token_used(db, email_token.id)

    user = await crud.get_user_by_id(db, email_token.user_id)
    if user is None:
        return False, "User not found"

    user.is_email_verified = True
    await crud.update_user(db, user)
    await _audit(db, AuditEventType.EMAIL_VERIFIED, user.id, request)
    logger.info("auth.email_verified", user_id=str(user.id))
    return True, None


async def resend_verification_email(
    db: AsyncSession,
    user_id: uuid.UUID,
    request: Optional[Request] = None,
) -> Tuple[bool, Optional[str]]:
    """Resend the email verification token."""
    user = await crud.get_user_by_id(db, user_id)
    if user is None:
        return False, "User not found"
    if user.is_email_verified:
        return False, "Email is already verified"

    # Invalidate old verification tokens
    await crud.invalidate_tokens_for_user_purpose(
        db, user.id, EmailTokenPurpose.VERIFY_EMAIL
    )

    raw_token = secrets.token_urlsafe(32)
    email_token = EmailToken(
        user_id=user.id,
        purpose=EmailTokenPurpose.VERIFY_EMAIL,
        token_hash=hash_token(raw_token),
        expires_at=utcnow() + timedelta(hours=24),
    )
    await crud.create_email_token(db, email_token)
    await _audit(db, AuditEventType.EMAIL_VERIFICATION_SENT, user.id, request)
    await _send_email_stub(user.email, "Verify your email", raw_token)
    return True, None


# ---------------------------------------------------------------------------
# Change password (authenticated)
# ---------------------------------------------------------------------------


async def change_password(
    db: AsyncSession,
    user_id: uuid.UUID,
    current_password: str,
    new_password: str,
    request: Optional[Request] = None,
) -> Tuple[bool, Optional[str]]:
    """Change password for an authenticated user."""
    credential = await crud.get_password_credential(db, user_id)
    if credential is None:
        return False, "No password credential found"

    if not verify_password(current_password, credential.password_hash):
        return False, "Current password is incorrect"

    await crud.update_password_hash(db, user_id, hash_password(new_password))
    await _audit(db, AuditEventType.PASSWORD_CHANGED, user_id, request)

    logger.info("auth.password_changed", user_id=str(user_id))
    return True, None


# ---------------------------------------------------------------------------
# Profile update
# ---------------------------------------------------------------------------


async def update_profile(
    db: AsyncSession,
    user_id: uuid.UUID,
    display_name: Optional[str] = None,
    new_email: Optional[str] = None,
    request: Optional[Request] = None,
) -> Tuple[Optional[User], Optional[str]]:
    """
    Update user profile fields.
    If email is changed, a confirmation token is created (re-verification required).
    """
    user = await crud.get_user_by_id(db, user_id)
    if user is None:
        return None, "User not found"

    if display_name is not None:
        user.display_name = display_name

    if new_email is not None and new_email != user.email:
        # Check if new email is already taken
        existing = await crud.get_user_by_email(db, new_email)
        if existing and existing.id != user.id:
            return None, "Email already in use"

        # Create a change-email token
        await crud.invalidate_tokens_for_user_purpose(
            db, user.id, EmailTokenPurpose.CHANGE_EMAIL
        )
        raw_token = secrets.token_urlsafe(32)
        email_token = EmailToken(
            user_id=user.id,
            purpose=EmailTokenPurpose.CHANGE_EMAIL,
            token_hash=hash_token(raw_token),
            new_email=new_email,
            expires_at=utcnow() + timedelta(hours=24),
        )
        await crud.create_email_token(db, email_token)
        await _send_email_stub(new_email, "Confirm your new email", raw_token)

    await crud.update_user(db, user)
    return user, None
