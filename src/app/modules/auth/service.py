"""Business logic of the auth module.

Registration, login with lockout, refresh-token sessions, password reset and
change, email verification, the email change and the admin panel actions
(one audited transaction per user or session). Orchestrates crud, password
hashing, token creation, audit logging and the email tasks. Failures are
raised as the module's `AppError` subclasses (`app.modules.auth.errors`).

Email tokens are issued in two steps. The request stores the token row
with the hash of a random value that is never sent, commits, and enqueues
the mail task with the token's id only. The worker (`prepare_token_email`)
gives that token a fresh value, commits, and mails it. No token, address
or username passes through Redis. The security notices (account locked,
email change requested or completed) are enqueued the same way, with a
user's or a token's id, and the worker reads the rest
(`prepare_account_locked_notice`, `prepare_email_change_notice`).
"""

import secrets
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import NoReturn

from fastapi import Request
from prometheus_client import Counter
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value
from taskiq.exceptions import SendTaskError

from app.core import (
    constraint_name,
    create_access_token,
    equalize_password_timing,
    get_logger,
    get_settings,
    hash_password,
    hash_token,
    login_admission_limiter,
    password_needs_rehash,
    utcnow,
    verify_password,
    wait_like_password_verify,
)
from app.modules.auth import crud
from app.modules.auth.errors import (
    EmailAlreadyVerifiedError,
    EmailTakenError,
    EmailUnchangedError,
    InvalidCredentialsError,
    InvalidRefreshTokenError,
    InvalidTokenError,
    SessionNotFoundError,
    UserExistsError,
    WrongPasswordError,
)
from app.modules.auth.models import (
    AuditEventType,
    AuthAuditLog,
    EmailToken,
    EmailTokenPurpose,
    PasswordCredential,
    Session,
)
from app.modules.auth.schemas import is_username
from app.modules.auth.tasks import (
    send_account_locked_notice_task,
    send_email_change_completed_notice_task,
    send_email_change_requested_notice_task,
    send_token_email_task,
)
from app.modules.users import User
from app.modules.users import crud as users_crud
from app.modules.users.models import (
    EMAIL_UNIQUE_CONSTRAINT,
    USERNAME_UNIQUE_INDEX,
    lock_is_active,
)

logger = get_logger(__name__)

LOGIN_FAILURES = Counter(
    "auth_login_failures_total",
    "Refused password logins by reason.",
    ["reason"],
)
LOGIN_FAILURE_REASONS = ("invalid_password", "unknown_user", "locked", "inactive")
for _reason in LOGIN_FAILURE_REASONS:
    # Export every reason from the start, so rate() sees the first failure.
    LOGIN_FAILURES.labels(reason=_reason)
LOCKOUTS = Counter(
    "auth_lockouts_total", "Accounts locked by too many failed password logins."
)
REFRESH_TOKEN_REUSE = Counter(
    "auth_refresh_token_reuse_total",
    "Rotated refresh tokens presented again; each revoked its session family.",
)

# Header values are client-controlled; cut them to the column sizes in
# models.py (sessions, auth_audit_log) before storing them.
_USER_AGENT_MAX_LENGTH = 512
_DEVICE_HEADER_MAX_LENGTH = 255

# Unique constraints of `users` -> the 409 message of the pre-check.
_REGISTER_CONFLICT_MESSAGES = {
    USERNAME_UNIQUE_INDEX: "Username already taken",
    EMAIL_UNIQUE_CONSTRAINT: "Email already registered",
}

# Validity of the links in the emails, counted from the request. A retried
# or redelivered mail task keeps this expiry, so a late retry sends nothing.
_RESET_TOKEN_LIFETIME = timedelta(hours=1)
_EMAIL_TOKEN_LIFETIME = timedelta(hours=24)
# Audit details are written when the mail is requested; whether it was sent
# is logged by the worker (`email.sent`, `email.not_sent`).
_VERIFICATION_REQUESTED = "Verification email requested"

# At most one account-locked mail per user in this time. A guessing attack
# can lock an account again every `LOGIN_LOCKOUT_SECONDS` (default 15 min,
# at least 1 s); one mail a day tells the user without flooding the inbox or
# letting anyone who knows a username send that user mail at will.
_LOCK_NOTICE_INTERVAL = timedelta(hours=24)

# Rows per delete statement of the cleanup, each batch its own transaction:
# short row locks and a bounded amount of WAL per commit, also when a large
# backlog is cleaned up the first time.
CLEANUP_BATCH_SIZE = 1_000
# Batches per table and run (100,000 rows). Each batch takes milliseconds,
# so a run stays far below the broker's 600 s redelivery idle time, after
# which another worker would start the same run; a larger backlog is
# finished by the following nights.
CLEANUP_MAX_BATCHES = 100


def _bounded_header(request: Request | None, name: str, max_length: int) -> str | None:
    """Return a request header cut to `max_length` characters (or None)."""
    if request is None:
        return None
    value = request.headers.get(name)
    return value[:max_length] if value is not None else None


async def _audit(
    db: AsyncSession,
    event: AuditEventType,
    user_id: uuid.UUID | None,
    request: Request | None = None,
    detail: str | None = None,
    *,
    ip_address: str | None = None,
) -> None:
    """Add an immutable audit-log entry to the caller's transaction.

    The client IP comes from `request`, or from `ip_address` for callers
    without one (the admin panel actions).
    """
    if request is not None and request.client is not None:
        ip_address = request.client.host
    log = AuthAuditLog(
        user_id=user_id,
        event_type=event,
        ip_address=ip_address,
        user_agent=_bounded_header(request, "user-agent", _USER_AGENT_MAX_LENGTH),
        detail=detail,
    )
    await crud.create_audit_log(db, log)


async def _open_email_token(
    db: AsyncSession,
    user_id: uuid.UUID,
    purpose: EmailTokenPurpose,
    valid_for: timedelta,
    new_email: str | None = None,
) -> uuid.UUID:
    """Replace the user's open tokens of `purpose` by a new one; no commit.

    The stored hash belongs to a random value that is never sent: the mail
    task sets the hash of the value it mails (`prepare_token_email`), so
    the raw token never has to be enqueued. Lock order: `email_tokens`
    first. `created_at` comes from the application clock, like
    `users.password_changed_at`, which it is compared with at redemption.

    Returns:
        The new token's id, for `_enqueue_token_email` after the commit.
    """
    token_id = uuid.uuid7()
    await crud.invalidate_tokens_for_user_purpose(db, user_id, purpose)
    await crud.create_email_token(
        db,
        EmailToken(
            id=token_id,
            user_id=user_id,
            purpose=purpose,
            token_hash=hash_token(secrets.token_urlsafe(32)),
            new_email=new_email,
            created_at=utcnow(),
            expires_at=utcnow() + valid_for,
        ),
    )
    return token_id


async def _enqueue_email(
    kiq: Callable[[str], Awaitable[object]],
    row_id: uuid.UUID,
    *,
    user_id: uuid.UUID,
    purpose: str,
) -> None:
    """Enqueue a mail task with one row id.

    Call it after the commit: the worker must find the row. A broker that
    cannot take the task is logged, not raised: the request has succeeded,
    and its answer must not depend on the mail. Only `row_id` is enqueued;
    `user_id` and `purpose` are for the log.

    Args:
        kiq: The task's `kiq` method.
        row_id: The email token's id, or the user's id for the lock notice.
        user_id: The account the mail is about.
        purpose: The mail's purpose, for the log.
    """
    try:
        await kiq(str(row_id))
    except SendTaskError as exc:
        cause = exc.__cause__ or exc
        logger.error(
            "auth.email_enqueue_failed",
            user_id=str(user_id),
            purpose=purpose,
            error_type=type(cause).__name__,
        )


async def _enqueue_token_email(
    token_id: uuid.UUID, user_id: uuid.UUID, purpose: EmailTokenPurpose
) -> None:
    """Enqueue the mail of one email token; see `_enqueue_email`."""
    await _enqueue_email(
        send_token_email_task.kiq, token_id, user_id=user_id, purpose=purpose.value
    )


async def _record_lockout(
    db: AsyncSession,
    user_id: uuid.UUID,
    attempts: int,
    request: Request | None,
    detail: str,
) -> None:
    """Audit, log and count a lock set by failed passwords; notify the user.

    Called by the one request whose failure reached `LOGIN_MAX_ATTEMPTS`.
    Commits the caller's transaction with the audit row before the notice
    is enqueued, and the caller answers the same way whether the enqueue
    worked or not.
    """
    LOCKOUTS.inc()
    await _audit(db, AuditEventType.ACCOUNT_LOCKED, user_id, request, detail)
    await db.commit()
    logger.warning("auth.account_locked", user_id=str(user_id), attempts=attempts)
    await _enqueue_email(
        send_account_locked_notice_task.kiq,
        user_id,
        user_id=user_id,
        purpose="account_locked",
    )


async def register_user(
    db: AsyncSession,
    username: str,
    email: str,
    password: str,
    display_name: str | None = None,
    request: Request | None = None,
) -> User:
    """Register a new user and send the verification email.

    The pre-checks answer the common case cheaply; the unique constraints
    decide parallel registrations (the loser gets the same 409). The user,
    its password credential, the verification token and the audit rows
    commit together or not at all.

    Returns:
        The new user.

    Raises:
        UserExistsError: The username or the email is already registered.
    """
    if await users_crud.get_user_by_username(db, username):
        raise UserExistsError("Username already taken")
    if await users_crud.get_user_by_email(db, email):
        raise UserExistsError("Email already registered")

    password_hash = await hash_password(password)

    user = User(
        username=username,
        email=email,
        display_name=display_name,
        is_email_verified=False,
    )
    try:
        await users_crud.create_user(db, user)
    except IntegrityError as exc:
        await db.rollback()
        constraint = constraint_name(exc)
        message = _REGISTER_CONFLICT_MESSAGES.get(constraint or "")
        if message is None:
            raise
        # Constraint name only: the error text contains the email/username.
        logger.info("auth.register_conflict", constraint=constraint)
        raise UserExistsError(message) from None

    credential = PasswordCredential(
        user_id=user.id,
        password_hash=password_hash,
    )
    await crud.create_password_credential(db, credential)

    token_id = await _open_email_token(
        db, user.id, EmailTokenPurpose.VERIFY_EMAIL, _EMAIL_TOKEN_LIFETIME
    )
    await _audit(db, AuditEventType.REGISTER, user.id, request)
    await _audit(
        db,
        AuditEventType.EMAIL_VERIFICATION_SENT,
        user.id,
        request,
        _VERIFICATION_REQUESTED,
    )
    await db.commit()
    await _enqueue_token_email(token_id, user.id, EmailTokenPurpose.VERIFY_EMAIL)

    logger.info("auth.user_registered", user_id=str(user.id))
    return user


async def _record_locked_login(
    db: AsyncSession,
    user_id: uuid.UUID,
    locked_until: datetime | None,
    request: Request | None,
) -> None:
    """Audit, log and count a login attempt on a locked account.

    Commits the audit row: the caller raises the refusal next, which would
    roll it back.
    """
    LOGIN_FAILURES.labels(reason="locked").inc()
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
    await db.commit()
    logger.warning(
        "auth.login_attempt_locked_account",
        user_id=str(user_id),
        remaining_seconds=remaining,
    )


async def _refuse_locked_login() -> NoReturn:
    """Refuse a guess on a locked account like a wrong password.

    Same answer and, without running Argon2, about the same duration, so
    neither reveals that the account exists or is locked.
    """
    await wait_like_password_verify()
    raise InvalidCredentialsError()


async def authenticate_user(  # noqa: PLR0912, PLR0915 - the lock checks, their order and the timing equalisation stay reviewable in one place
    db: AsyncSession,
    username: str,
    password: str,
    request: Request | None = None,
) -> User:
    """Verify credentials. Handles account locking on repeated failures.

    Every refusal (unknown user, wrong password, locked or inactive account)
    raises the same `InvalidCredentialsError`. A name that is not a
    well-formed username counts as unknown.

    Burst safety: the credential check runs under an admission limiter, it
    re-reads the lock state after the wait, and a failure is counted before
    the slot is released. Once one guess of a burst has locked the account,
    later guesses are refused without an Argon2 evaluation. The success
    path is a conditional UPDATE that refuses if a lock, or a password
    change or reset, was committed while the password was being verified.
    """
    settings = get_settings()
    # A name outside the pattern is never looked up: `lower()` in the
    # database could fold it onto another user's ASCII username.
    user = (
        await users_crud.get_user_by_username(db, username)
        if is_username(username)
        else None
    )
    if user is None:
        await equalize_password_timing(password)
        LOGIN_FAILURES.labels(reason="unknown_user").inc()
        logger.warning("auth.login_attempt_unknown_user")
        raise InvalidCredentialsError()

    # Locked account: is_locked with locked_until NULL is indefinite (admin),
    # a timed lock ends once locked_until lies in the past.
    if user.is_locked:
        if user.is_lock_active():
            await _record_locked_login(db, user.id, user.locked_until, request)
            await _refuse_locked_login()

        # The timed lock has expired: clear it, unless a parallel guess
        # locked the account again since the check above.
        if await crud.clear_expired_lock(db, user.id):
            await _audit(
                db,
                AuditEventType.ACCOUNT_UNLOCKED,
                user.id,
                request,
                "Lock expired",
            )
            logger.info("auth.account_unlocked", user_id=str(user.id))
        # Commit before the password check: the UPDATE holds the user row
        # lock, which must not last over Argon2, and the cleared lock stays
        # cleared whatever this attempt's outcome.
        await db.commit()

    if not user.is_active:
        await equalize_password_timing(password)
        LOGIN_FAILURES.labels(reason="inactive").inc()
        logger.warning("auth.login_attempt_inactive_account", user_id=str(user.id))
        raise InvalidCredentialsError()

    locked_meanwhile = False
    stored: tuple[str, datetime | None] | None = None
    failure: tuple[int, bool, datetime | None] | None = None
    async with login_admission_limiter():
        # Re-check after the wait: guesses evaluated meanwhile may have
        # locked the account (fresh read, not the cached User).
        lock_state = await crud.get_lock_state(db, user.id)
        if lock_state is None:
            LOGIN_FAILURES.labels(reason="unknown_user").inc()
            raise InvalidCredentialsError()
        if lock_is_active(*lock_state):
            await _record_locked_login(db, user.id, lock_state[1], request)
            locked_meanwhile = True
        else:
            # The hash and its version in one read: the success write below
            # is conditional on that version.
            stored = await crud.get_password_version(db, user.id)
            if stored is None:
                await equalize_password_timing(password)
                valid = False
            else:
                valid = await verify_password(password, stored[0])

            if not valid:
                # Atomic increment + lock in the database (no lost update),
                # committed before the admission slot is released, so the
                # guesses queued behind this one see the lock.
                failure = await crud.register_failed_login(
                    db,
                    user.id,
                    settings.LOGIN_MAX_ATTEMPTS,
                    settings.LOGIN_LOCKOUT_SECONDS,
                ) or (0, False, None)
                await db.commit()

    if locked_meanwhile:
        # Outside the limiter: the timing wait must not hold an admission slot.
        await _refuse_locked_login()

    if failure is not None or stored is None:
        attempts, is_locked, _locked_until = failure or (0, False, None)
        LOGIN_FAILURES.labels(reason="invalid_password").inc()
        logger.warning("auth.login_failed", user_id=str(user.id), attempts=attempts)
        # Exactly one request reaches the threshold; it records the lock.
        if is_locked and attempts == settings.LOGIN_MAX_ATTEMPTS:
            await _record_lockout(
                db,
                user.id,
                attempts,
                request,
                f"Locked after {attempts} failed attempts",
            )
        else:
            await _audit(db, AuditEventType.LOGIN_FAILED, user.id, request)
            await db.commit()
        raise InvalidCredentialsError()

    # Success: reset the counters, unless a parallel guess locked the account
    # or the password changed while it was being verified (conditional
    # UPDATE). No timing wait on a refusal: this request has already run a
    # real verify, so it already takes as long as a wrong password.
    password_hash, password_changed_at = stored
    last_login_at = await crud.record_successful_login(db, user.id, password_changed_at)
    if last_login_at is None:
        lock_state = await crud.get_lock_state(db, user.id)
        if lock_state is None or lock_is_active(*lock_state):
            await _record_locked_login(
                db, user.id, lock_state[1] if lock_state else None, request
            )
        else:
            await _record_outdated_password_login(db, user.id, request)
        raise InvalidCredentialsError()
    # Mirror the written row into the cached object without marking it
    # dirty, so a later flush cannot write these values a second time.
    set_committed_value(user, "failed_login_attempts", 0)
    set_committed_value(user, "is_locked", False)
    set_committed_value(user, "locked_until", None)
    set_committed_value(user, "last_login_at", last_login_at)
    await _audit(db, AuditEventType.LOGIN_SUCCESS, user.id, request)
    await db.commit()

    # After the commit: the reset above holds the user row lock until then,
    # and it must not last over the Argon2 hashing.
    if password_needs_rehash(password_hash):
        await _rehash_password(db, user.id, password, password_hash)

    logger.info("auth.login_success", user_id=str(user.id))
    return user


async def _record_outdated_password_login(
    db: AsyncSession, user_id: uuid.UUID, request: Request | None
) -> None:
    """Audit, log and count a login whose password changed while it was checked.

    The password was right when it was read, so the failure counter stays:
    a reset racing the real user's login must not count towards a lockout.
    Commits the audit row before the caller raises the refusal.
    """
    LOGIN_FAILURES.labels(reason="invalid_password").inc()
    await _audit(
        db,
        AuditEventType.LOGIN_FAILED,
        user_id,
        request,
        "Password changed during login",
    )
    await db.commit()
    logger.warning("auth.login_credentials_changed", user_id=str(user_id))


async def _rehash_password(
    db: AsyncSession, user_id: uuid.UUID, password: str, old_hash: str
) -> None:
    """Store `password` hashed with the configured Argon2 parameters.

    Best effort: the login has already committed, so a failure keeps the
    old hash and the login goes on. Only `old_hash` is replaced, so a
    password change or reset committed meanwhile is never overwritten.
    The savepoint confines a failed write to itself; rolling back the whole
    session would expire the loaded `User` the login still returns.
    """
    try:
        new_hash = await hash_password(password)
        async with db.begin_nested():
            rehashed = await crud.update_password_hash(
                db, user_id, new_hash, expected_hash=old_hash
            )
    except Exception as exc:  # noqa: BLE001 - a failed rehash never fails the login
        logger.warning(
            "auth.password_rehash_failed",
            user_id=str(user_id),
            error_type=type(exc).__name__,
        )
        return
    await db.commit()
    if rehashed:
        logger.info("auth.password_rehashed", user_id=str(user_id))


@dataclass(frozen=True)
class SessionFamily:
    """The rotation family of one login: its id and its absolute end."""

    id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class SessionTokens:
    """The tokens of a login or a rotation, for the response."""

    access_token: str
    refresh_token: str
    # Lifetime of the access token in seconds, as encoded in its `exp`.
    expires_in: int


async def create_session_tokens(
    db: AsyncSession,
    user: User,
    request: Request | None = None,
    family: SessionFamily | None = None,
) -> SessionTokens:
    """Create an access token and a refresh token (persisted as a session).

    Without `family` (a login) the session starts a new family that ends
    `SESSION_MAX_LIFETIME_DAYS` from now. A rotation passes the family of
    the rotated session; the new session expires after
    `REFRESH_TOKEN_EXPIRE_DAYS`, and the access token after
    `ACCESS_TOKEN_EXPIRE_MINUTES`, but neither after the family's end.

    Commits the caller's transaction with the new session; for a rotation
    that is the same commit as the consumed predecessor.

    Returns:
        The access token, the raw refresh token and the access token's
        lifetime.
    """
    settings = get_settings()
    now = utcnow()
    if family is None:
        family = SessionFamily(
            id=uuid.uuid7(),
            expires_at=now + timedelta(days=settings.SESSION_MAX_LIFETIME_DAYS),
        )
    access_lifetime = max(
        timedelta(0),
        min(
            timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
            family.expires_at - now,
        ),
    )
    access_token = create_access_token(
        user.username, str(user.id), str(family.id), access_lifetime
    )

    # The raw token goes to the client only; the session stores its hash.
    raw_refresh = secrets.token_urlsafe(32)
    session = Session(
        user_id=user.id,
        token_hash=hash_token(raw_refresh),
        family_id=family.id,
        expires_at=min(
            now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            family.expires_at,
        ),
        family_expires_at=family.expires_at,
        device_id=_bounded_header(request, "x-device-id", _DEVICE_HEADER_MAX_LENGTH),
        device_name=_bounded_header(
            request, "x-device-name", _DEVICE_HEADER_MAX_LENGTH
        ),
        ip_address=request.client.host if request and request.client else None,
        user_agent=_bounded_header(request, "user-agent", _USER_AGENT_MAX_LENGTH),
    )
    await crud.create_session(db, session)
    await db.commit()

    return SessionTokens(
        access_token=access_token,
        refresh_token=raw_refresh,
        expires_in=int(access_lifetime.total_seconds()),
    )


async def refresh_session(
    db: AsyncSession,
    raw_token: str,
    request: Request | None = None,
) -> SessionTokens:
    """Rotate a refresh token. PostgreSQL is the only gate (atomic UPDATE).

    Detects token reuse and revokes the whole family.

    The rotation runs under the per-user lock (`crud.lock_user`): a
    revocation of this user (logout-all, password reset or change, admin
    lock) waits until the successor session is committed and then revokes
    it as well. Lock order: users, then sessions.

    Returns:
        The tokens of the successor session.

    Raises:
        InvalidRefreshTokenError: The token is unknown, expired (its family
            ended included), revoked or reused, or the account is inactive
            or admin-locked.
    """
    token_hash_val = hash_token(raw_token)

    owner = await crud.get_session_owner(db, token_hash_val)
    if owner is None:
        raise InvalidRefreshTokenError()
    owner_id, family_id = owner

    user = await crud.lock_and_get_user(db, owner_id)
    # Only the indefinite (admin) lock ends sessions; a timed lock from
    # failed logins blocks the password login only.
    if user is None or not user.is_active or user.is_admin_locked():
        # The presented session is revoked as "account_disabled", never
        # "rotated", so a retry with the same cookie is a plain 401 and not a
        # reuse signal.
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
        # Before the refusal, which would roll the revocation back.
        await db.commit()
        raise InvalidRefreshTokenError()

    family_expires_at = await crud.rotate_session(db, token_hash_val)
    if family_expires_at is None:
        sess = await crud.get_session_by_token_hash(db, token_hash_val)
        if sess is not None and sess.is_revoked and sess.revoke_reason == "rotated":
            await _revoke_reused_family(db, sess, request)
        await db.rollback()  # end the transaction: releases the user lock
        raise InvalidRefreshTokenError()

    # Same transaction as the rotation: create_session_tokens commits the
    # old and the new session together.
    tokens = await create_session_tokens(
        db, user, request, SessionFamily(family_id, family_expires_at)
    )
    logger.info("auth.token_refreshed")
    return tokens


async def _revoke_reused_family(
    db: AsyncSession, sess: Session, request: Request | None
) -> NoReturn:
    """Handle a rotated refresh token presented again: revoke its family.

    A rotated token in use is a theft signal (RFC 9700 section 4.14.2), on
    refresh and on logout alike.

    Raises:
        InvalidRefreshTokenError: Always.
    """
    REFRESH_TOKEN_REUSE.inc()
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
    # Before the refusal, which would roll the revocation back.
    await db.commit()
    raise InvalidRefreshTokenError(
        "Token reuse detected – all sessions revoked"  # noqa: RUF001 - response text, unchanged
    )


async def logout_session(
    db: AsyncSession,
    raw_token: str,
    request: Request | None = None,
) -> None:
    """Revoke the session of a refresh token (logout).

    Only an active session is revoked, under the user lock, so the reason
    of a revoked one survives. Logging out a session that is revoked
    already is a no-op, except for a rotated token: that is reuse, handled
    like a refresh with it.

    Raises:
        SessionNotFoundError: No session has this token.
        InvalidRefreshTokenError: The token was rotated already (reuse); its
            family is revoked.
    """
    token_hash_val = hash_token(raw_token)
    owner = await crud.get_session_owner(db, token_hash_val)
    if owner is None:
        raise SessionNotFoundError()
    await crud.lock_user(db, owner[0])
    sess = await crud.get_session_by_token_hash(db, token_hash_val)
    if sess is None:
        await db.rollback()
        raise SessionNotFoundError()
    if sess.is_revoked:
        if sess.revoke_reason == "rotated":
            await _revoke_reused_family(db, sess, request)
        await db.rollback()
        return
    await crud.revoke_active_session(db, sess.id, reason="logout")
    await _audit(
        db,
        AuditEventType.SESSION_REVOKED,
        sess.user_id,
        request,
        "Logout",
    )
    await db.commit()
    logger.info("auth.user_logged_out")


async def logout_all_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
    request: Request | None = None,
) -> int:
    """Revoke every session of the user (all devices).

    Returns:
        The number of revoked sessions.
    """
    revoked = await crud.revoke_user_sessions(db, user_id, reason="logout_all")
    await _audit(
        db,
        AuditEventType.SESSION_REVOKED,
        user_id,
        request,
        f"Logout all, revoked {revoked} session(s)",
    )
    await db.commit()
    logger.info("auth.logout_all", user_id=str(user_id), revoked_sessions=revoked)
    return revoked


async def forgot_password(
    db: AsyncSession,
    email: str,
    request: Request | None = None,
) -> None:
    """Initiate a password reset.

    Always returns normally, so the caller cannot tell whether the address
    belongs to an account. An inactive or admin-locked account gets no
    token and no mail, only an audit row: `verify_email` refuses the
    tokens of such accounts as well. The mail goes to the stored address,
    never to the request input. The response time still differs between
    known and unknown addresses (README, Known issues).
    """
    user = await users_crud.get_user_by_email(db, email.lower())
    if user is None:
        return

    if not user.is_active or user.is_admin_locked():
        reason = "inactive" if not user.is_active else "locked"
        await _audit(
            db,
            AuditEventType.PASSWORD_RESET_REQUESTED,
            user.id,
            request,
            f"No email sent: account {reason}",
        )
        await db.commit()
        logger.warning(
            "auth.password_reset_refused_account_blocked", user_id=str(user.id)
        )
        return

    token_id = await _open_email_token(
        db, user.id, EmailTokenPurpose.RESET_PASSWORD, _RESET_TOKEN_LIFETIME
    )
    await _audit(
        db,
        AuditEventType.PASSWORD_RESET_REQUESTED,
        user.id,
        request,
        "Password reset email requested",
    )
    await db.commit()
    await _enqueue_token_email(token_id, user.id, EmailTokenPurpose.RESET_PASSWORD)


async def reset_password(
    db: AsyncSession,
    raw_token: str,
    new_password: str,
    request: Request | None = None,
) -> None:
    """Complete the password-reset flow.

    Revokes every session of the user and every other open email token: a
    pending email change or verification started by whoever knew the old
    password ends here. The redemption, the new hash and the revocations
    commit together, so nothing the old password does meanwhile survives
    the reset, and a failure before the commit changes nothing.

    Raises:
        InvalidTokenError: The token is unknown, used, invalidated or
            expired, also when it was redeemed while the new password was
            being hashed.
    """
    token_hash = hash_token(raw_token)
    # Checked before hashing, so an invalid token never costs Argon2.
    if not await crud.email_token_is_open(
        db, token_hash, EmailTokenPurpose.RESET_PASSWORD
    ):
        raise InvalidTokenError("Invalid or expired reset token")
    # Ends the read transaction: no transaction stays open over Argon2.
    await db.rollback()
    new_hash = await hash_password(new_password)

    consumed = await crud.consume_email_token(
        db, token_hash, EmailTokenPurpose.RESET_PASSWORD
    )
    if consumed is None:
        raise InvalidTokenError("Invalid or expired reset token")
    _token_id, user_id, _purpose, _new_email = consumed
    # Lock order email_tokens -> users -> sessions -> password_credentials.
    await crud.invalidate_open_email_tokens(db, user_id)
    revoked = await crud.revoke_user_sessions(db, user_id, reason="password_reset")
    await crud.mark_password_changed(db, user_id)
    await crud.update_password_hash(db, user_id, new_hash)
    await _audit(
        db,
        AuditEventType.PASSWORD_RESET_COMPLETED,
        user_id,
        request,
        f"Revoked {revoked} session(s)",
    )
    await db.commit()
    logger.info(
        "auth.password_reset_completed",
        user_id=str(user_id),
        revoked_sessions=revoked,
    )


async def verify_email(
    db: AsyncSession,
    raw_token: str,
    request: Request | None = None,
) -> None:
    """Redeem a verification or email-change token.

    A verification token marks the address verified. An email-change token
    replaces the address with the confirmed new one, which this step also
    verifies. The redemption commits together with its effect.

    A token of an inactive or admin-locked account is refused like an
    invalid one and spent, so lifting the block later does not revive a
    link that may be in the wrong hands; the user requests a new one.

    Raises:
        InvalidTokenError: The token is unknown, used, invalidated or
            expired, or its account is inactive or admin-locked.
        EmailTakenError: Another account took the new address meanwhile.
    """
    consumed = await crud.consume_email_token(
        db,
        hash_token(raw_token),
        EmailTokenPurpose.VERIFY_EMAIL,
        EmailTokenPurpose.CHANGE_EMAIL,
    )
    if consumed is None:
        raise InvalidTokenError("Invalid or expired verification token")
    token_id, user_id, purpose, new_email = consumed
    # Parallel requests can leave a second open token of this purpose; it
    # ends with this redemption (email_tokens first in the lock order).
    await crud.invalidate_tokens_for_user_purpose(db, user_id, purpose)

    user = await users_crud.get_user_by_id(db, user_id)
    if user is None:
        await db.rollback()
        raise InvalidTokenError("Invalid or expired verification token")
    if not user.is_active or user.is_admin_locked():
        await db.commit()  # spends the token
        logger.warning("auth.email_token_refused_account_blocked", user_id=str(user_id))
        raise InvalidTokenError("Invalid or expired verification token")

    if purpose is EmailTokenPurpose.CHANGE_EMAIL:
        if not new_email:
            await db.rollback()
            raise InvalidTokenError("Invalid or expired verification token")
        await _apply_email_change(db, user, token_id, new_email, request)
        return

    user.is_email_verified = True
    await users_crud.update_user(db, user)
    await _audit(db, AuditEventType.EMAIL_VERIFIED, user.id, request)
    await db.commit()
    logger.info("auth.email_verified", user_id=str(user.id))


async def _apply_email_change(
    db: AsyncSession,
    user: User,
    token_id: uuid.UUID,
    new_email: str,
    request: Request | None,
) -> None:
    """Switch `user` to the confirmed `new_email`; commits the redemption.

    Uniqueness is checked again because the address may have been taken
    since the change was requested; the unique constraint decides a race.
    Reset links mailed to the old address stop working in the same commit.
    If the old address was verified, the token keeps it (`previous_email`)
    for the notice to it, enqueued after the commit; an address nobody
    confirmed gets no mail. A conflict rolls everything back.
    """
    existing = await users_crud.get_user_by_email(db, new_email)
    if existing is not None and existing.id != user.id:
        await db.rollback()
        raise EmailTakenError()

    # email_tokens before users (lock order).
    await crud.invalidate_tokens_for_user_purpose(
        db, user.id, EmailTokenPurpose.RESET_PASSWORD
    )
    if user.is_email_verified:
        await crud.set_email_token_previous_email(db, token_id, user.email)
    user.email = new_email
    user.is_email_verified = True
    try:
        await users_crud.update_user(db, user)
    except IntegrityError as exc:
        await db.rollback()
        constraint = constraint_name(exc)
        if constraint != EMAIL_UNIQUE_CONSTRAINT:
            raise
        # Constraint name only: the error text contains the email.
        logger.info("auth.email_change_conflict", constraint=constraint)
        raise EmailTakenError() from None
    await _audit(db, AuditEventType.EMAIL_CHANGED, user.id, request)
    await db.commit()
    logger.info("auth.email_changed", user_id=str(user.id))
    await _enqueue_email(
        send_email_change_completed_notice_task.kiq,
        token_id,
        user_id=user.id,
        purpose="email_change_completed",
    )


async def resend_verification_email(
    db: AsyncSession,
    user: User,
    request: Request | None = None,
) -> None:
    """Send a new verification token; older ones stop working.

    Raises:
        EmailAlreadyVerifiedError: The address is verified already.
    """
    if user.is_email_verified:
        raise EmailAlreadyVerifiedError()

    token_id = await _open_email_token(
        db, user.id, EmailTokenPurpose.VERIFY_EMAIL, _EMAIL_TOKEN_LIFETIME
    )
    await _audit(
        db,
        AuditEventType.EMAIL_VERIFICATION_SENT,
        user.id,
        request,
        _VERIFICATION_REQUESTED,
    )
    await db.commit()
    await _enqueue_token_email(token_id, user.id, EmailTokenPurpose.VERIFY_EMAIL)
    logger.info("auth.verification_email_resent", user_id=str(user.id))


@dataclass(frozen=True)
class TokenEmail:
    """What the mail task needs to send one token email."""

    user_id: uuid.UUID
    purpose: EmailTokenPurpose
    recipient: str
    username: str
    token: str


async def prepare_token_email(
    db: AsyncSession, token_id: uuid.UUID
) -> TokenEmail | None:
    """Give one open email token a fresh value for one mail.

    The worker side of the token emails. Every run, a retry or a redelivery
    included, sets a new value, so only the latest mail's link works. The
    expiry stays as the request set it. One transaction under the token's
    row lock: nothing is committed unless the new hash is.

    Nothing is sent, and None returned, when the token is no longer open
    (expired, redeemed, or cancelled by a password change, an admin action,
    a newer request or the redemption of another token of its purpose),
    when the account is inactive or admin-locked, and for a verification
    mail when the address is verified already. A token skipped for the
    account's state is invalidated, so a redelivery after the block is
    lifted does not mail a link requested while it was in place.

    Args:
        db: The task's session; committed or rolled back here.
        token_id: The token the request created.

    Returns:
        Owner, purpose, recipient, username and raw token, or None if
        nothing must be sent. The recipient is the stored address, or the
        requested new address for an email change.
    """
    token = await crud.lock_open_email_token(db, token_id)
    user = await users_crud.get_user_by_id(db, token.user_id) if token else None
    if token is None or user is None:
        await db.rollback()
        logger.info("email.not_sent", token_id=str(token_id), reason="no_open_token")
        return None
    user_id, purpose = token.user_id, token.purpose

    skip_reason: str | None = None
    if not user.is_active or user.is_admin_locked():
        skip_reason = "account_blocked"
    elif purpose is EmailTokenPurpose.VERIFY_EMAIL and user.is_email_verified:
        skip_reason = "already_verified"
    elif purpose is EmailTokenPurpose.CHANGE_EMAIL and not token.new_email:
        skip_reason = "no_new_email"
    if skip_reason is not None:
        await crud.invalidate_email_token(db, token.id)
        await db.commit()
        logger.info(
            "email.not_sent",
            token_id=str(token_id),
            user_id=str(user_id),
            purpose=purpose.value,
            reason=skip_reason,
        )
        return None

    raw_token = secrets.token_urlsafe(32)
    await crud.set_email_token_hash(db, token.id, hash_token(raw_token))
    await db.commit()
    recipient = (
        token.new_email
        if purpose is EmailTokenPurpose.CHANGE_EMAIL and token.new_email
        else user.email
    )
    return TokenEmail(
        user_id=user_id,
        purpose=purpose,
        recipient=recipient,
        username=user.username,
        token=raw_token,
    )


@dataclass(frozen=True)
class AccountLockedNotice:
    """What the lock notice task needs to tell a user about a timed lock."""

    user_id: uuid.UUID
    recipient: str
    username: str
    locked_until: datetime


async def prepare_account_locked_notice(
    db: AsyncSession, user_id: uuid.UUID
) -> AccountLockedNotice | None:
    """Decide whether to tell a user that failed passwords locked the account.

    The worker side of the lock notice; it only reads. Nothing is sent, and
    None returned, when the account is gone or inactive, when its address
    was never verified (it may not belong to the user), when the timed lock
    has ended (expired, or lifted by an admin), when an admin lock replaced
    it (the mail would state a wrong end), or when a lock notice was sent to
    this user within `_LOCK_NOTICE_INTERVAL`. The interval counts the
    `account_locked_notice_sent` audit rows, which only a sent notice
    writes, so admin locks and notices that were never sent do not count.

    Args:
        db: The task's session; its read transaction ends here.
        user_id: The account a failed password locked.

    Returns:
        Recipient (the account's address), username and the end of the
        lock, or None if nothing must be sent.
    """
    user = await users_crud.get_user_by_id(db, user_id)
    skip_reason: str | None = None
    if user is None:
        skip_reason = "no_user"
    elif not user.is_active:
        skip_reason = "account_inactive"
    elif not user.is_email_verified:
        skip_reason = "unverified_address"
    elif user.is_admin_locked():
        skip_reason = "admin_locked"
    elif not user.is_lock_active():
        skip_reason = "not_locked"
    elif await crud.audit_event_since(
        db,
        user_id,
        AuditEventType.ACCOUNT_LOCKED_NOTICE_SENT,
        utcnow() - _LOCK_NOTICE_INTERVAL,
    ):
        skip_reason = "recently_notified"
    notice: AccountLockedNotice | None = None
    if user is not None and user.locked_until is not None and skip_reason is None:
        notice = AccountLockedNotice(
            user_id=user.id,
            recipient=user.email,
            username=user.username,
            locked_until=user.locked_until,
        )
    # After reading the user: a rollback expires loaded rows.
    await db.rollback()
    if notice is None:
        logger.info(
            "email.not_sent",
            user_id=str(user_id),
            purpose="account_locked",
            reason=skip_reason,
        )
    return notice


async def record_account_locked_notice(
    db: AsyncSession, notice: AccountLockedNotice
) -> None:
    """Audit a sent lock notice; the row throttles the next one. Commits."""
    await _audit(
        db,
        AuditEventType.ACCOUNT_LOCKED_NOTICE_SENT,
        notice.user_id,
        detail=f"Lock notice sent, locked until {notice.locked_until.isoformat()}",
    )
    await db.commit()


@dataclass(frozen=True)
class EmailChangeNotice:
    """What a notice task needs to tell the old address about an email change."""

    user_id: uuid.UUID
    recipient: str
    username: str


async def prepare_email_change_notice(
    db: AsyncSession, token_id: uuid.UUID, *, completed: bool
) -> EmailChangeNotice | None:
    """Find the old address to tell about an email change.

    The worker side of the two notices; it only reads. The completed notice
    goes to the token's `previous_email`. The requested notice goes to the
    address the account had when the change was requested: the token's
    `previous_email` once this change is confirmed, otherwise the current
    address. Only verified addresses get a notice: `previous_email` is
    stored only for a verified old address, and an unverified current
    address is skipped (`unverified_address`). Nothing is sent either when
    the token or the account is gone, the account is inactive, or a
    completed notice finds no confirmed change. A pending, expired or
    replaced token does not stop the requested notice: the request happened
    all the same.

    Args:
        db: The task's session; its read transaction ends here.
        token_id: The email-change token the request created.
        completed: True for the notice after the confirmation.

    Returns:
        Owner, recipient and username, or None if nothing must be sent.
    """
    token = await crud.get_email_token(db, token_id)
    user = await users_crud.get_user_by_id(db, token.user_id) if token else None
    skip_reason: str | None = None
    notice: EmailChangeNotice | None = None
    if token is None or token.purpose is not EmailTokenPurpose.CHANGE_EMAIL:
        skip_reason = "no_token"
    elif user is None:
        skip_reason = "no_user"
    elif not user.is_active:
        skip_reason = "account_inactive"
    elif token.used_at is not None and (
        token.previous_email is not None or user.email == token.new_email
    ):
        # This change was confirmed. Without `previous_email` the replaced
        # address was unverified, and the current one is the new address.
        if token.previous_email is None:
            skip_reason = "unverified_address"
        else:
            notice = EmailChangeNotice(
                user_id=user.id,
                recipient=token.previous_email,
                username=user.username,
            )
    elif completed:
        skip_reason = "not_completed"
    elif not user.is_email_verified:
        skip_reason = "unverified_address"
    else:
        notice = EmailChangeNotice(
            user_id=user.id, recipient=user.email, username=user.username
        )
    # After reading the rows: a rollback expires loaded rows.
    await db.rollback()
    if notice is None:
        logger.info(
            "email.not_sent",
            token_id=str(token_id),
            purpose="email_change_completed" if completed else "email_change_requested",
            reason=skip_reason,
        )
    return notice


@dataclass(frozen=True)
class CleanupResult:
    """What one cleanup run deleted, and whether it stopped at the cap."""

    sessions_deleted: int
    email_tokens_deleted: int
    capped: bool


async def cleanup_ended_auth_records(
    db: AsyncSession,
    retention: timedelta,
    *,
    batch_size: int = CLEANUP_BATCH_SIZE,
    max_batches: int = CLEANUP_MAX_BATCHES,
) -> CleanupResult:
    """Delete ended sessions and closed email tokens older than `retention`.

    A session counts as ended when its family has ended
    (`crud.delete_ended_sessions`), so a rotated token stays detectable
    while its family can still be refreshed. Sessions first, then email
    tokens, each in batches of `batch_size` rows
    with one transaction per batch, until a batch comes back short or
    `max_batches` batches ran; the next run continues. No user row is
    locked, and rows locked by others are skipped, so the cleanup never
    waits on a request; a skipped row goes in the next run. The audit log
    is not touched.

    Args:
        db: The task's session; committed after every batch.
        retention: How long a row is kept after it ended or last changed.
        batch_size: Rows per delete statement.
        max_batches: Batches per table in this run.

    Returns:
        The deleted rows per table, and whether a table hit `max_batches`.
    """
    totals: list[int] = []
    capped = False
    for delete_batch in (crud.delete_ended_sessions, crud.delete_closed_email_tokens):
        total = 0
        for _ in range(max_batches):
            deleted = await delete_batch(db, retention, batch_size)
            await db.commit()
            total += deleted
            if deleted < batch_size:
                break
        else:
            capped = True
        totals.append(total)
    return CleanupResult(
        sessions_deleted=totals[0], email_tokens_deleted=totals[1], capped=capped
    )


async def _confirm_password(
    db: AsyncSession,
    user_id: uuid.UUID,
    password: str,
    request: Request | None,
) -> datetime | None:
    """Check the current password of a signed-in user like a login attempt.

    Same counter, threshold and timed lock as `authenticate_user`, and like
    there a lock does not end sessions. While the account is locked even
    the correct password is refused, without an Argon2 evaluation. A
    correct password resets the counter, as a successful login does.

    Commits its writes, a refusal included: a refusal is raised and would
    roll them back, and a success must release the user row lock of the
    reset before the caller hashes a new password or touches `email_tokens`
    (lock order email_tokens -> users).

    Returns:
        The credential version (`users.password_changed_at`) read together
        with the verified hash, for a write that must not overwrite a newer
        password.

    Raises:
        WrongPasswordError: The password is wrong, the user has no password
            credential, or the account is locked.
    """
    settings = get_settings()
    lock_state = await crud.get_lock_state(db, user_id)
    if lock_state is None or lock_is_active(*lock_state):
        await _audit(
            db,
            AuditEventType.LOGIN_FAILED,
            user_id,
            request,
            "Password confirmation, account locked",
        )
        await db.commit()
        await wait_like_password_verify()
        raise WrongPasswordError()

    stored = await crud.get_password_version(db, user_id)
    if stored is None:
        await equalize_password_timing(password)
        valid = False
    else:
        valid = await verify_password(password, stored[0])

    if stored is None or not valid:
        attempts, is_locked, _locked_until = await crud.register_failed_login(
            db, user_id, settings.LOGIN_MAX_ATTEMPTS, settings.LOGIN_LOCKOUT_SECONDS
        ) or (0, False, None)
        logger.warning(
            "auth.password_confirmation_failed", user_id=str(user_id), attempts=attempts
        )
        # Exactly one request reaches the threshold; it records the lock.
        if is_locked and attempts == settings.LOGIN_MAX_ATTEMPTS:
            await _record_lockout(
                db,
                user_id,
                attempts,
                request,
                f"Locked after {attempts} failed attempts (password confirmation)",
            )
        else:
            await _audit(
                db,
                AuditEventType.LOGIN_FAILED,
                user_id,
                request,
                "Password confirmation",
            )
            await db.commit()
        raise WrongPasswordError()

    # A lock committed by a parallel wrong guess meanwhile still wins.
    if not await crud.reset_failed_logins(db, user_id):
        await _audit(
            db,
            AuditEventType.LOGIN_FAILED,
            user_id,
            request,
            "Password confirmation, account locked",
        )
        await db.commit()
        raise WrongPasswordError()
    await db.commit()
    return stored[1]


async def change_password(
    db: AsyncSession,
    user_id: uuid.UUID,
    current_password: str,
    new_password: str,
    request: Request | None = None,
    keep_session_family: uuid.UUID | None = None,
) -> None:
    """Change password for an authenticated user.

    Revokes every session except the caller's family (`keep_session_family`,
    the access token's `sid`) and invalidates every open email token
    (reset, verification, pending email change). Without a `sid` (token
    issued before the claim existed) all sessions are revoked. The current
    password is checked like a login (`_confirm_password`).

    A password change or reset committed after the confirmation wins: the
    request then changes nothing and is refused like a wrong password,
    without counting towards the lockout (the password was right when it
    was checked).

    Raises:
        WrongPasswordError: `current_password` is wrong, the user has no
            password credential, the account is locked, or the password
            changed since it was confirmed.
    """
    seen = await _confirm_password(db, user_id, current_password, request)

    # Hashed before any lock is taken. The open email tokens, the other
    # sessions and the password change in one commit (lock order
    # email_tokens -> users -> sessions -> password_credentials).
    new_hash = await hash_password(new_password)
    await crud.invalidate_open_email_tokens(db, user_id)
    revoked = await crud.revoke_user_sessions(
        db, user_id, reason="password_changed", except_family=keep_session_family
    )
    # Every password write holds the user lock (taken by the revocation)
    # or, for the rehash, is conditional on the old hash; so once the
    # version is still `seen` here, the hash below cannot overwrite a newer
    # password.
    if not await crud.mark_password_changed_if_unchanged(db, user_id, seen):
        await db.rollback()
        await _audit(
            db,
            AuditEventType.LOGIN_FAILED,
            user_id,
            request,
            "Password confirmation, password changed meanwhile",
        )
        await db.commit()
        logger.warning("auth.password_change_credentials_changed", user_id=str(user_id))
        raise WrongPasswordError()
    await crud.update_password_hash(db, user_id, new_hash)
    await _audit(
        db,
        AuditEventType.PASSWORD_CHANGED,
        user_id,
        request,
        f"Revoked {revoked} other session(s)",
    )
    await db.commit()

    logger.info("auth.password_changed", user_id=str(user_id), revoked_sessions=revoked)


async def request_email_change(
    db: AsyncSession,
    user: User,
    current_password: str,
    new_email: str,
    request: Request | None = None,
) -> None:
    """Start an email change: mail a confirmation link to the new address.

    The account keeps its current address until the link is redeemed
    (`verify_email`); that address gets a notice that a change was
    requested. A new request invalidates the previous link. The
    password is checked first, so only the account holder learns whether an
    address is registered.

    Raises:
        WrongPasswordError: `current_password` is wrong, the user has no
            password credential, or the account is locked.
        EmailUnchangedError: `new_email` is the current address.
        EmailTakenError: `new_email` belongs to another account.
    """
    user_id = user.id
    seen = await _confirm_password(db, user_id, current_password, request)
    if new_email == user.email:
        raise EmailUnchangedError()
    if await users_crud.get_user_by_email(db, new_email) is not None:
        raise EmailTakenError()

    token_id = await _open_email_token(
        db,
        user_id,
        EmailTokenPurpose.CHANGE_EMAIL,
        _EMAIL_TOKEN_LIFETIME,
        new_email=new_email,
    )
    # A reset or change committed since the confirmation ends this request:
    # it may have invalidated the open tokens before this one existed. The
    # user lock (after email_tokens in the lock order) waits for a reset in
    # flight; a reset that takes it later stamps a `password_changed_at`
    # after this token's `created_at`, which the redemption refuses.
    current = await crud.lock_and_get_user(db, user_id)
    if current is None or current.password_changed_at != seen:
        await db.rollback()
        await _audit(
            db,
            AuditEventType.LOGIN_FAILED,
            user_id,
            request,
            "Password confirmation, password changed meanwhile",
        )
        await db.commit()
        logger.warning("auth.email_change_credentials_changed", user_id=str(user_id))
        raise WrongPasswordError()
    await _audit(
        db,
        AuditEventType.EMAIL_VERIFICATION_SENT,
        user_id,
        request,
        "Email change requested",
    )
    await db.commit()
    await _enqueue_token_email(token_id, user_id, EmailTokenPurpose.CHANGE_EMAIL)
    await _enqueue_email(
        send_email_change_requested_notice_task.kiq,
        token_id,
        user_id=user_id,
        purpose="email_change_requested",
    )
    logger.info("auth.email_change_requested", user_id=str(user_id))


async def _lock_user_or_skip(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """Take the user row lock for an admin action; None if the user is gone.

    A missing user ends the (empty) transaction, so the caller can return.
    """
    user = await crud.lock_and_get_user(db, user_id)
    if user is None:
        await db.rollback()
    return user


async def admin_lock_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    actor: str,
    ip_address: str | None = None,
) -> bool:
    """Lock an account indefinitely and end all its sessions and email tokens.

    The indefinite lock (`locked_until` NULL) also rejects the user's access
    tokens and refresh, unlike a timed lock from failed logins. A pending
    email change or verification is cancelled. One transaction, lock order
    email_tokens -> users -> sessions.

    Args:
        db: Session for this user's transaction; committed or rolled back here.
        user_id: The account to lock.
        actor: Username of the acting admin, recorded in the audit detail.
        ip_address: Client IP of the admin's request, for the audit row.

    Returns:
        False if the user does not exist (nothing changed), otherwise True.
    """
    await crud.invalidate_open_email_tokens(db, user_id)
    user = await _lock_user_or_skip(db, user_id)
    if user is None:
        return False
    user.is_locked = True
    user.locked_until = None
    revoked = await crud.revoke_user_sessions(db, user_id, reason="admin_lock")
    await _audit(
        db,
        AuditEventType.ACCOUNT_LOCKED,
        user_id,
        detail=f"Locked by admin {actor}, revoked {revoked} session(s)",
        ip_address=ip_address,
    )
    await db.commit()
    logger.info("admin.user_locked", user_id=str(user_id))
    return True


async def admin_unlock_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    actor: str,
    ip_address: str | None = None,
) -> bool:
    """Lift any lock and reset the failed-login counter.

    Args:
        db: Session for this user's transaction; committed or rolled back here.
        user_id: The account to unlock.
        actor: Username of the acting admin, recorded in the audit detail.
        ip_address: Client IP of the admin's request, for the audit row.

    Returns:
        False if the user does not exist (nothing changed), otherwise True.
    """
    user = await _lock_user_or_skip(db, user_id)
    if user is None:
        return False
    user.is_locked = False
    user.locked_until = None
    user.failed_login_attempts = 0
    await _audit(
        db,
        AuditEventType.ACCOUNT_UNLOCKED,
        user_id,
        detail=f"Unlocked by admin {actor}",
        ip_address=ip_address,
    )
    await db.commit()
    logger.info("admin.user_unlocked", user_id=str(user_id))
    return True


async def admin_deactivate_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    actor: str,
    ip_address: str | None = None,
) -> bool:
    """Deactivate an account and end all its sessions and email tokens.

    An inactive account cannot log in, and its access tokens and refresh
    are rejected. A pending email change or verification is cancelled. One
    transaction, lock order email_tokens -> users -> sessions.

    Args:
        db: Session for this user's transaction; committed or rolled back here.
        user_id: The account to deactivate.
        actor: Username of the acting admin, recorded in the audit detail.
        ip_address: Client IP of the admin's request, for the audit row.

    Returns:
        False if the user does not exist (nothing changed), otherwise True.
    """
    await crud.invalidate_open_email_tokens(db, user_id)
    user = await _lock_user_or_skip(db, user_id)
    if user is None:
        return False
    user.is_active = False
    revoked = await crud.revoke_user_sessions(db, user_id, reason="admin_deactivate")
    await _audit(
        db,
        AuditEventType.ACCOUNT_DEACTIVATED,
        user_id,
        detail=f"Deactivated by admin {actor}, revoked {revoked} session(s)",
        ip_address=ip_address,
    )
    await db.commit()
    logger.info("admin.user_deactivated", user_id=str(user_id))
    return True


async def admin_reactivate_user(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    actor: str,
    ip_address: str | None = None,
) -> bool:
    """Reactivate a deactivated account. A lock, if any, stays.

    Args:
        db: Session for this user's transaction; committed or rolled back here.
        user_id: The account to reactivate.
        actor: Username of the acting admin, recorded in the audit detail.
        ip_address: Client IP of the admin's request, for the audit row.

    Returns:
        False if the user does not exist (nothing changed), otherwise True.
    """
    user = await _lock_user_or_skip(db, user_id)
    if user is None:
        return False
    user.is_active = True
    await _audit(
        db,
        AuditEventType.ACCOUNT_REACTIVATED,
        user_id,
        detail=f"Reactivated by admin {actor}",
        ip_address=ip_address,
    )
    await db.commit()
    logger.info("admin.user_reactivated", user_id=str(user_id))
    return True


async def admin_force_password_change(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    actor: str,
    ip_address: str | None = None,
) -> bool:
    """Set `must_change` on the user's password credential.

    Only the flag is set; nothing enforces it yet (login, refresh and
    access tokens ignore it). Lock order users -> password_credentials.

    Args:
        db: Session for this user's transaction; committed or rolled back here.
        user_id: The account whose password must change.
        actor: Username of the acting admin, recorded in the audit detail.
        ip_address: Client IP of the admin's request, for the audit row.

    Returns:
        False if the user or its password credential does not exist
        (nothing changed), otherwise True.
    """
    if await _lock_user_or_skip(db, user_id) is None:
        return False
    if not await crud.set_password_must_change(db, user_id):
        await db.rollback()
        return False
    await _audit(
        db,
        AuditEventType.PASSWORD_CHANGE_FORCED,
        user_id,
        detail=f"Password change forced by admin {actor}",
        ip_address=ip_address,
    )
    await db.commit()
    logger.info("admin.password_change_forced", user_id=str(user_id))
    return True


async def admin_revoke_session(
    db: AsyncSession,
    session_id: uuid.UUID,
    *,
    actor: str,
    ip_address: str | None = None,
) -> bool:
    """Revoke one refresh session (reason `admin`) and audit it.

    Takes the owner's user lock first (lock order users -> sessions), so a
    rotation of this session in flight commits before. If it did, the
    session is already revoked as `rotated` and its successor stays active;
    the admin then revokes the successor or locks the user.

    Args:
        db: Session for this transaction; committed or rolled back here.
        session_id: The session to revoke.
        actor: Username of the acting admin, recorded in the audit detail.
        ip_address: Client IP of the admin's request, for the audit row.

    Returns:
        False if the session does not exist or was already revoked (nothing
        changed), otherwise True.
    """
    user_id = await crud.get_session_user_id(db, session_id)
    if user_id is None:
        await db.rollback()
        return False
    await crud.lock_user(db, user_id)
    if not await crud.revoke_active_session(db, session_id, reason="admin"):
        await db.rollback()
        return False
    await _audit(
        db,
        AuditEventType.SESSION_REVOKED,
        user_id,
        detail=f"Session {session_id} revoked by admin {actor}",
        ip_address=ip_address,
    )
    await db.commit()
    logger.info("admin.session_revoked", user_id=str(user_id))
    return True
