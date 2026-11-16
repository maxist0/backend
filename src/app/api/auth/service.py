# src/app/api/auth/service.py

"""
Business logic for authentication.
Orchestrates CRUD operations, password hashing, token creation, and audit logging.
"""

import secrets
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Optional, Tuple

import anyio
from anyio.lowlevel import RunVar
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.api.auth import crud
from app.utils.jwt_utils import create_access_token
from app.api.auth.models import (
    AuditEventType,
    AuthAuditLog,
    EmailToken,
    EmailTokenPurpose,
    PasswordCredential,
    Session,
    User,
    hash_token,
    lock_is_active,
    utcnow,
)
from app.api.auth.schemas import UserResponse
from app.common.config import settings
from app.common.constants import MAXIMUM_LOGIN_ATTEMPTS, LOGIN_ATTEMPT_WINDOW_SECONDS
from app.common.db_errors import constraint_name
from app.common.logging import get_logger
from app.utils.email_tasks import (
    send_verification_email_task,
    send_password_reset_email_task,
)

logger = get_logger(__name__)

# Header values are client-controlled; cut them to the column sizes in
# src/app/api/auth/models.py (sessions/auth_audit_log) before storing them.
_USER_AGENT_MAX_LENGTH = 512
_DEVICE_HEADER_MAX_LENGTH = 255

# Unique constraints of ``users`` (PostgreSQL default names for the unnamed
# constraints in the initial migration) -> the 409 message of the pre-check.
_REGISTER_CONFLICT_MESSAGES = {
    "users_username_key": "Username already taken",
    "users_email_key": "Email already registered",
}


# ---------------------------------------------------------------------------
# Password helpers (Argon2id, executed outside the event loop)
# ---------------------------------------------------------------------------

# One limiter per event loop (RunVar), like anyio's own default thread limiter.
_password_hash_limiter_var: RunVar[anyio.CapacityLimiter] = RunVar(
    "password_hash_limiter"
)


def _password_hash_limiter() -> anyio.CapacityLimiter:
    """Bound concurrent Argon2 work: each operation needs ARGON2_MEMORY_KIB."""
    try:
        return _password_hash_limiter_var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(settings.PASSWORD_HASH_CONCURRENCY)
        _password_hash_limiter_var.set(limiter)
        return limiter


_login_admission_limiter_var: RunVar[anyio.CapacityLimiter] = RunVar(
    "login_admission_limiter"
)


def _login_admission_limiter() -> anyio.CapacityLimiter:
    """Bound concurrent login credential checks (lock re-check + verify).

    Same capacity as the Argon2 limiter: a login is admitted only when it can
    be evaluated, and it re-reads the lock state after the wait. Separate
    from the Argon2 limiter because ``verify_password`` takes that one
    itself (a task cannot hold two tokens of one limiter). Admission holders
    wait for Argon2 slots, never the other way round: no deadlock.
    """
    try:
        return _login_admission_limiter_var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(settings.PASSWORD_HASH_CONCURRENCY)
        _login_admission_limiter_var.set(limiter)
        return limiter


@lru_cache(maxsize=1)
def _password_hasher() -> PasswordHasher:
    """Argon2id hasher configured from Settings (created on first use)."""
    return PasswordHasher(
        time_cost=settings.ARGON2_TIME_COST,
        memory_cost=settings.ARGON2_MEMORY_KIB,
        parallelism=settings.ARGON2_PARALLELISM,
    )


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """Hash of a random password, used to equalise timing for unknown users."""
    return _password_hasher().hash(secrets.token_urlsafe(16))


def _verify_password_sync(password: str, password_hash: str) -> bool:
    try:
        return _password_hasher().verify(password_hash, password)
    except VerificationError, InvalidHashError:
        return False


def _burn_verify_sync(password: str) -> None:
    _verify_password_sync(password, _dummy_password_hash())


async def hash_password(password: str) -> str:
    """Return an argon2id hash of *password* (computed in a worker thread)."""
    return await anyio.to_thread.run_sync(
        _password_hasher().hash, password, limiter=_password_hash_limiter()
    )


async def verify_password(password: str, password_hash: str) -> bool:
    """Return True if *password* matches *password_hash* (worker thread)."""
    return await anyio.to_thread.run_sync(
        _verify_password_sync,
        password,
        password_hash,
        limiter=_password_hash_limiter(),
    )


def password_needs_rehash(password_hash: str) -> bool:
    """True if *password_hash* was made with other parameters than configured."""
    return _password_hasher().check_needs_rehash(password_hash)


async def _equalize_password_timing(password: str) -> None:
    """Spend one verify when no real verify happens (unknown/inactive user).

    Keeps the response time of those branches close to a wrong-password
    attempt, so timing does not reveal whether an account exists.
    """
    await anyio.to_thread.run_sync(
        _burn_verify_sync, password, limiter=_password_hash_limiter()
    )


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
        profile_picture_url="",  # Placeholder for profile picture URL
        is_active=user.is_active,
        is_email_verified=user.is_email_verified,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        roles=["test"],  # Placeholder for role-based access control
        permissions=["posts:create", "posts:read"],  # Placeholder for permissions
        preferences={
            "locale": "en-US",
            "theme": "dark",
            "timezone": "UTC",
        },
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
# Request helpers
# ---------------------------------------------------------------------------


def _bounded_header(
    request: Optional[Request], name: str, max_length: int
) -> Optional[str]:
    """Return a request header cut to *max_length* characters (or None)."""
    if request is None:
        return None
    value = request.headers.get(name)
    return value[:max_length] if value is not None else None


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
        user_agent=_bounded_header(request, "user-agent", _USER_AGENT_MAX_LENGTH),
        detail=detail,
    )
    await crud.create_audit_log(db, log)


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

    The pre-checks answer the common case cheaply; the unique constraints
    decide parallel registrations (the loser gets the same 409).
    """
    # Check uniqueness
    if await crud.get_user_by_username(db, username):
        return None, "Username already taken"
    if await crud.get_user_by_email(db, email):
        return None, "Email already registered"

    password_hash = await hash_password(password)

    # Create user
    user = User(
        username=username,
        email=email,
        display_name=display_name,
        is_email_verified=False,
    )
    try:
        await crud.create_user(db, user)
    except IntegrityError as exc:
        await db.rollback()
        constraint = constraint_name(exc)
        message = _REGISTER_CONFLICT_MESSAGES.get(constraint or "")
        if message is None:
            raise
        # Constraint name only: the error text contains the email/username.
        logger.info("auth.register_conflict", constraint=constraint)
        return None, message

    # Create password credential
    credential = PasswordCredential(
        user_id=user.id,
        password_hash=password_hash,
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

    # Enqueue verification email (fire-and-forget via Taskiq)
    await send_verification_email_task.kiq(user.email, username, raw_token)

    logger.info("auth.user_registered", user_id=str(user.id))
    return user, None


# ---------------------------------------------------------------------------
# Authentication (login)
# ---------------------------------------------------------------------------


async def _reject_locked_login(
    db: AsyncSession,
    user_id: uuid.UUID,
    locked_until: Optional[datetime],
    request: Optional[Request],
) -> Tuple[None, str]:
    """Audit and answer a login attempt on a locked account."""
    remaining = (
        max(int((locked_until - utcnow()).total_seconds()), 0)
        if locked_until is not None
        else None
    )
    await _audit(
        db,
        AuditEventType.LOGIN_FAILED,
        user_id,
        request,
        "Account locked"
        if remaining is None
        else f"Account locked, {remaining}s remaining",
    )
    logger.warning(
        "auth.login_attempt_locked_account",
        user_id=str(user_id),
        remaining_seconds=remaining,
    )
    if remaining is None:
        return None, "Account is locked"
    return None, f"Account is locked. Try again in {remaining} seconds"


async def authenticate_user(
    db: AsyncSession,
    username: str,
    password: str,
    request: Optional[Request] = None,
) -> Tuple[Optional[User], Optional[str]]:
    """
    Verify credentials. Handles account locking on repeated failures.
    Returns (User, None) on success or (None, error_message) on failure.

    Burst safety: the credential check runs under an admission limiter, it
    re-reads the lock state after the wait, and a failure is counted before
    the slot is released. Once one guess of a burst has locked the account,
    later guesses are refused without an Argon2 evaluation. The success
    path is a conditional UPDATE that refuses if a lock was committed while
    the password was being verified.
    """
    user = await crud.get_user_by_username(db, username)
    if user is None:
        await _equalize_password_timing(password)
        logger.warning("auth.login_attempt_unknown_user")
        return None, "Invalid username or password"

    # Locked account: is_locked with locked_until NULL is indefinite (admin),
    # a timed lock ends once locked_until lies in the past.
    if user.is_locked:
        if user.is_lock_active():
            return await _reject_locked_login(db, user.id, user.locked_until, request)

        # Timed lock expired → unlock
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
        logger.info("auth.account_unlocked", user_id=str(user.id))

    if not user.is_active:
        await _equalize_password_timing(password)
        logger.warning("auth.login_attempt_inactive_account", user_id=str(user.id))
        return None, "Account is deactivated"

    async with _login_admission_limiter():
        # Re-check after the wait: guesses evaluated meanwhile may have
        # locked the account (fresh read, not the cached User).
        lock_state = await crud.get_lock_state(db, user.id)
        if lock_state is None:
            return None, "Invalid username or password"
        if lock_is_active(*lock_state):
            return await _reject_locked_login(db, user.id, lock_state[1], request)

        credential = await crud.get_password_credential(db, user.id)
        if credential is None:
            await _equalize_password_timing(password)
            valid = False
        else:
            valid = await verify_password(password, credential.password_hash)

        failure: Optional[tuple[int, bool, Optional[datetime]]] = None
        if not valid:
            # Atomic increment + lock in the database (no lost update),
            # committed before the admission slot is released.
            failure = await crud.register_failed_login(
                db, user.id, MAXIMUM_LOGIN_ATTEMPTS, LOGIN_ATTEMPT_WINDOW_SECONDS
            ) or (0, False, None)

    if failure is not None or credential is None:
        attempts, is_locked, _locked_until = failure or (0, False, None)
        logger.warning("auth.login_failed", user_id=str(user.id), attempts=attempts)
        if is_locked:
            # Exactly one request reaches the threshold; it records the lock.
            if attempts == MAXIMUM_LOGIN_ATTEMPTS:
                await _audit(
                    db,
                    AuditEventType.ACCOUNT_LOCKED,
                    user.id,
                    request,
                    f"Locked after {attempts} failed attempts",
                )
                logger.warning(
                    "auth.account_locked", user_id=str(user.id), attempts=attempts
                )
            else:
                await _audit(db, AuditEventType.LOGIN_FAILED, user.id, request)
            return None, "Account has been locked due to too many failed login attempts"
        await _audit(db, AuditEventType.LOGIN_FAILED, user.id, request)
        return None, "Invalid username or password"

    # Success → reset counters, unless a parallel guess locked the account
    # while this password was being verified (conditional UPDATE).
    last_login_at = await crud.record_successful_login(db, user.id)
    if last_login_at is None:
        lock_state = await crud.get_lock_state(db, user.id)
        return await _reject_locked_login(
            db, user.id, lock_state[1] if lock_state else None, request
        )
    # Mirror the committed row into the cached object without marking it
    # dirty, so a later flush cannot write these values a second time.
    set_committed_value(user, "failed_login_attempts", 0)
    set_committed_value(user, "is_locked", False)
    set_committed_value(user, "locked_until", None)
    set_committed_value(user, "last_login_at", last_login_at)

    # Upgrade the hash if the Argon2 parameters in Settings changed.
    if password_needs_rehash(credential.password_hash):
        await crud.update_password_hash(db, user.id, await hash_password(password))
        logger.info("auth.password_rehashed", user_id=str(user.id))

    await _audit(db, AuditEventType.LOGIN_SUCCESS, user.id, request)
    logger.info("auth.login_success", user_id=str(user.id))
    return user, None


# ---------------------------------------------------------------------------
# Session / Refresh-token management
# ---------------------------------------------------------------------------


async def create_session_tokens(
    db: AsyncSession,
    user: User,
    request: Optional[Request] = None,
    family_id: Optional[uuid.UUID] = None,
) -> Tuple[str, str]:
    """
    Create an access token + refresh token (persisted as a DB session).
    Returns (access_token, raw_refresh_token). Commits.
    """
    fam_id = family_id or uuid.uuid4()
    access_token = create_access_token(user.username, str(user.id), str(fam_id))

    # Generate opaque refresh token; only its SHA-256 hash is stored.
    raw_refresh = secrets.token_urlsafe(32)
    session = Session(
        user_id=user.id,
        token_hash=hash_token(raw_refresh),
        family_id=fam_id,
        expires_at=utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        device_id=_bounded_header(request, "x-device-id", _DEVICE_HEADER_MAX_LENGTH),
        device_name=_bounded_header(
            request, "x-device-name", _DEVICE_HEADER_MAX_LENGTH
        ),
        ip_address=request.client.host if request and request.client else None,
        user_agent=_bounded_header(request, "user-agent", _USER_AGENT_MAX_LENGTH),
    )
    await crud.create_session(db, session)

    return access_token, raw_refresh


async def refresh_session(
    db: AsyncSession,
    raw_token: str,
    request: Optional[Request] = None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Rotate a refresh token. PostgreSQL is the only gate (atomic UPDATE).
    Detects token reuse and revokes the whole family.
    Returns (access_token, raw_refresh_token, None) on success,
    or (None, None, error_message) on failure.

    The rotation runs under the per-user lock (``crud.lock_user``): a
    revocation of this user (logout-all, password reset or change, admin
    lock) waits until the successor session is committed and then revokes
    it as well. Lock order: users, then sessions.
    """
    token_hash_val = hash_token(raw_token)

    owner = await crud.get_session_owner(db, token_hash_val)
    if owner is None:
        return None, None, "Invalid or expired refresh token"
    owner_id, family_id = owner

    user = await crud.lock_and_get_user(db, owner_id)
    # Only the indefinite (admin) lock ends sessions; a timed lock from
    # failed logins blocks the password login only.
    if user is None or not user.is_active or user.is_admin_locked():
        # The presented session is revoked as "account_disabled", never
        # "rotated", so a retry with the same cookie is a plain 401 and not a
        # reuse signal. revoke_family commits on its own.
        revoked = await crud.revoke_family(
            db, owner_id, family_id, reason="account_disabled"
        )
        logger.warning(
            "auth.refresh_denied_account_disabled",
            user_id=str(owner_id),
            revoked_sessions=revoked,
        )
        if user is not None and revoked:
            await _audit(
                db,
                AuditEventType.SESSION_REVOKED,
                user.id,
                request,
                f"Refresh denied: account inactive or locked, revoked {revoked} "
                "session(s)",
            )
        return None, None, "Invalid or expired refresh token"

    rotated = await crud.rotate_session(db, token_hash_val)
    if rotated is None:
        sess = await crud.get_session_by_token_hash(db, token_hash_val)
        if sess is not None and sess.is_revoked and sess.revoke_reason == "rotated":
            # A token that was already rotated is presented again: theft signal
            # (RFC 9700 section 4.14.2). Revoke the entire family.
            logger.warning(
                "auth.token_reuse_detected",
                family_id=str(sess.family_id),
                user_id=str(sess.user_id),
            )
            revoked = await crud.revoke_family(
                db, sess.user_id, sess.family_id, reason="reuse_detected"
            )
            await _audit(
                db,
                AuditEventType.SESSION_REVOKED,
                sess.user_id,
                request,
                f"Token reuse detected, revoked {revoked} session(s) "
                f"in family {sess.family_id}",
            )
            return None, None, "Token reuse detected – all sessions revoked"
        await db.rollback()  # end the transaction: releases the user lock
        return None, None, "Invalid or expired refresh token"

    # Same transaction as the rotation: one commit for old + new session.
    access_token, new_raw_refresh = await create_session_tokens(
        db, user, request, family_id=family_id
    )
    return access_token, new_raw_refresh, None


async def logout_session(
    db: AsyncSession,
    raw_token: str,
    request: Optional[Request] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Revoke the refresh token (logout).
    Returns (True, None) on success or (False, error_message) on failure.
    """
    token_hash_val = hash_token(raw_token)
    sess = await crud.get_session_by_token_hash(db, token_hash_val)
    if sess:
        await crud.revoke_session(db, sess.id, reason="logout")
        await _audit(
            db, AuditEventType.SESSION_REVOKED, sess.user_id, request, "Logout"
        )
        return True, None

    return False, "Session not found"


async def logout_all_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
    request: Optional[Request] = None,
) -> int:
    """Revoke every session of the user (all devices). Returns the count."""
    revoked = await crud.revoke_user_sessions(db, user_id, reason="logout_all")
    await _audit(
        db,
        AuditEventType.SESSION_REVOKED,
        user_id,
        request,
        f"Logout all, revoked {revoked} session(s)",
    )
    logger.info("auth.logout_all", user_id=str(user_id), revoked_sessions=revoked)
    return revoked


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
    # Send to the stored address, never to the request input.
    await send_password_reset_email_task.kiq(user.email, user.username, raw_token)


async def reset_password(
    db: AsyncSession,
    raw_token: str,
    new_password: str,
    request: Optional[Request] = None,
) -> Tuple[bool, Optional[str]]:
    """
    Complete the password-reset flow.
    Revokes every session of the user and all other open reset tokens.
    Returns (True, None) on success or (False, error_message) on failure.
    """
    consumed = await crud.consume_email_token(
        db, hash_token(raw_token), EmailTokenPurpose.RESET_PASSWORD
    )
    if consumed is None:
        return False, "Invalid or expired reset token"
    user_id, _new_email = consumed

    # Fail closed: end sessions first (this also commits the token redemption),
    # then change the password.
    revoked = await crud.revoke_user_sessions(db, user_id, reason="password_reset")
    await crud.invalidate_tokens_for_user_purpose(
        db, user_id, EmailTokenPurpose.RESET_PASSWORD
    )
    await crud.update_password_hash(db, user_id, await hash_password(new_password))
    await _audit(
        db,
        AuditEventType.PASSWORD_RESET_COMPLETED,
        user_id,
        request,
        f"Revoked {revoked} session(s)",
    )
    logger.info(
        "auth.password_reset_completed",
        user_id=str(user_id),
        revoked_sessions=revoked,
    )
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
    consumed = await crud.consume_email_token(
        db, hash_token(raw_token), EmailTokenPurpose.VERIFY_EMAIL
    )
    if consumed is None:
        return False, "Invalid or expired verification token"
    user_id, _new_email = consumed

    user = await crud.get_user_by_id(db, user_id)
    if user is None:
        return False, "User not found"

    user.is_email_verified = True
    await crud.update_user(db, user)  # commits the token redemption as well
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
    await send_verification_email_task.kiq(user.email, user.username, raw_token)
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
    keep_session_family: Optional[uuid.UUID] = None,
) -> Tuple[bool, Optional[str]]:
    """Change password for an authenticated user.

    Revokes every session except the caller's family (``keep_session_family``,
    the access token's ``sid``) and invalidates open password-reset tokens.
    Without a ``sid`` (token issued before the claim existed) all sessions
    are revoked.
    """
    credential = await crud.get_password_credential(db, user_id)
    if credential is None:
        return False, "No password credential found"

    if not await verify_password(current_password, credential.password_hash):
        return False, "Current password is incorrect"

    new_hash = await hash_password(new_password)
    # Fail closed: end the other sessions before the password changes.
    revoked = await crud.revoke_user_sessions(
        db, user_id, reason="password_changed", except_family=keep_session_family
    )
    await crud.invalidate_tokens_for_user_purpose(
        db, user_id, EmailTokenPurpose.RESET_PASSWORD
    )
    await crud.update_password_hash(db, user_id, new_hash)
    await _audit(
        db,
        AuditEventType.PASSWORD_CHANGED,
        user_id,
        request,
        f"Revoked {revoked} other session(s)",
    )

    logger.info("auth.password_changed", user_id=str(user_id), revoked_sessions=revoked)
    return True, None
