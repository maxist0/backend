"""Data-access layer of the auth module.

Credentials, sessions, email tokens and the audit log, plus the lockout
counters and the per-user lock on `users`. PostgreSQL is the single source
of truth for sessions (refresh tokens); nothing is cached in Redis.

No function here commits or rolls back: the calling service owns the
transaction and decides when it ends.
"""

import uuid
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import (
    ColumnElement,
    and_,
    case,
    delete,
    func,
    not_,
    null,
    or_,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import get_logger, utcnow
from app.modules.auth.models import (
    AuditEventType,
    AuthAuditLog,
    EmailToken,
    EmailTokenPurpose,
    PasswordCredential,
    Session,
)
from app.modules.users import User

logger = get_logger(__name__)


async def lock_user(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Take the per-user lock that serialises session rotation and revocation.

    `SELECT ... FOR NO KEY UPDATE` on the user row, held until the caller's
    transaction ends. Every transaction that rotates or revokes refresh
    sessions takes it BEFORE it touches `sessions`, so they queue per user,
    and the next statement after the wait sees what the previous holder
    committed (READ COMMITTED takes a new snapshot per statement).

    Lock order everywhere: `email_tokens` -> `users` -> `sessions` ->
    `password_credentials`.
    `NO KEY UPDATE` (not `UPDATE`) leaves the `KEY SHARE` locks of
    foreign-key checks free: inserts into sessions or auth_audit_log for
    this user do not wait. No-op for an unknown user.
    """
    await db.execute(
        select(User.id).where(User.id == user_id).with_for_update(key_share=True)
    )


async def lock_and_get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """`lock_user` that also returns the current row (fresh, not cached).

    `populate_existing` overwrites an instance already in the session's
    identity map with the values read under the lock.
    """
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .with_for_update(key_share=True)
        .execution_options(populate_existing=True)
    )
    return result.scalars().first()


async def get_lock_state(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[bool, datetime | None] | None:
    """Read `(is_locked, locked_until)` from the database.

    Bypasses the identity map: a cached `User` would show the state of the
    first read.
    """
    result = await db.execute(
        select(User.is_locked, User.locked_until).where(User.id == user_id)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def record_successful_login(
    db: AsyncSession, user_id: uuid.UUID, password_changed_at: datetime | None
) -> datetime | None:
    """Reset the failure counter, unless locked or the password changed.

    One conditional `UPDATE ... RETURNING`: a lock committed by a parallel
    wrong guess after this request checked the lock still wins, and so does
    a password change or reset committed after this request read the hash
    it verified (`password_changed_at` is the value read with that hash).
    Under READ COMMITTED a row changed by a concurrent transaction is
    re-checked against the condition before it is updated. An expired
    timed lock is cleared. Returns the new `last_login_at`, or None when
    the account is locked, its password changed meanwhile, or it is gone.
    """
    lock_active = and_(
        User.is_locked.is_(True),
        or_(User.locked_until.is_(None), User.locked_until > func.now()),
    )
    result = await db.execute(
        update(User)
        .where(
            User.id == user_id,
            not_(lock_active),
            User.password_changed_at.is_not_distinct_from(password_changed_at),
        )
        .values(
            failed_login_attempts=0,
            is_locked=False,
            locked_until=None,
            last_login_at=func.now(),
            updated_at=func.now(),
        )
        .returning(User.last_login_at)
        .execution_options(synchronize_session=False)
    )
    row = result.first()
    return row[0] if row is not None else None


async def reset_failed_logins(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Reset the failure counter after a correct password, unless locked.

    The conditional `UPDATE` of `record_successful_login` without
    `last_login_at`: a lock committed meanwhile wins, an expired timed lock
    is cleared. Returns False when the account is locked (or gone).
    """
    lock_active = and_(
        User.is_locked.is_(True),
        or_(User.locked_until.is_(None), User.locked_until > func.now()),
    )
    result = await db.execute(
        update(User)
        .where(User.id == user_id, not_(lock_active))
        .values(
            failed_login_attempts=0,
            is_locked=False,
            locked_until=None,
            updated_at=func.now(),
        )
        .returning(User.id)
        .execution_options(synchronize_session=False)
    )
    return result.first() is not None


async def clear_expired_lock(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Lift a timed lock that has run out, and reset the failure counter.

    Conditional on the stored lock still being expired, so a lock committed
    after the caller's check is never erased. Expiry is judged on the
    application clock, like `User.is_lock_active`. Returns True if this
    call lifted the lock.
    """
    result = await db.execute(
        update(User)
        .where(
            User.id == user_id,
            User.is_locked.is_(True),
            User.locked_until.is_not(None),
            User.locked_until <= utcnow(),
        )
        .values(
            failed_login_attempts=0,
            is_locked=False,
            locked_until=None,
            updated_at=func.now(),
        )
        .returning(User.id)
        .execution_options(synchronize_session=False)
    )
    return result.first() is not None


async def register_failed_login(
    db: AsyncSession, user_id: uuid.UUID, max_attempts: int, lock_seconds: int
) -> tuple[int, bool, datetime | None] | None:
    """Count a failed login atomically and lock the account at the threshold.

    One `UPDATE ... RETURNING`: the increment happens in the database, so
    parallel failures cannot overwrite each other (no lost update). A timed
    lock (`locked_until`) is only set when the account is not locked yet,
    so an indefinite admin lock (`locked_until IS NULL`) is never weakened.

    An expired timed lock counts as no lock, and the failure is the first of
    a new series: only a successful login clears an expired lock, so without
    this, wrong password confirmations after a lock ran out would count on
    past the threshold with the old `locked_until` and never lock again.
    The request whose failure reaches `max_attempts` is still exactly one.

    Returns `(failed_login_attempts, is_locked, locked_until)` or None if
    the user does not exist.
    """
    # All SET expressions read the row as it was before this UPDATE.
    lock_expired = and_(
        User.is_locked.is_(True),
        User.locked_until.is_not(None),
        User.locked_until <= func.now(),
    )
    attempts = case((lock_expired, 1), else_=User.failed_login_attempts + 1)
    reaches_threshold = attempts >= max_attempts
    result = await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            failed_login_attempts=attempts,
            is_locked=case(
                (reaches_threshold, True),
                (lock_expired, False),
                else_=User.is_locked,
            ),
            locked_until=case(
                (
                    and_(
                        reaches_threshold,
                        or_(User.is_locked.is_(False), lock_expired),
                    ),
                    func.now() + timedelta(seconds=lock_seconds),
                ),
                (lock_expired, null()),
                else_=User.locked_until,
            ),
            updated_at=func.now(),
        )
        .returning(User.failed_login_attempts, User.is_locked, User.locked_until)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1], row[2]


async def create_password_credential(
    db: AsyncSession, credential: PasswordCredential
) -> PasswordCredential:
    """Add a password credential to the session."""
    db.add(credential)
    return credential


async def get_password_version(
    db: AsyncSession, user_id: uuid.UUID
) -> tuple[str, datetime | None] | None:
    """Return `(password_hash, users.password_changed_at)` in one statement.

    One statement reads both from one snapshot, so the version always
    belongs to the hash. None if the user has no password credential.
    """
    result = await db.execute(
        select(PasswordCredential.password_hash, User.password_changed_at)
        .join(User, User.id == PasswordCredential.user_id)
        .where(PasswordCredential.user_id == user_id)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def update_password_hash(
    db: AsyncSession,
    user_id: uuid.UUID,
    new_hash: str,
    *,
    expected_hash: str | None = None,
) -> bool:
    """Replace the user's password hash.

    With `expected_hash` only that hash is replaced, so a rehash never
    overwrites a password changed meanwhile. Returns True if a row changed.
    """
    conditions = [PasswordCredential.user_id == user_id]
    if expected_hash is not None:
        conditions.append(PasswordCredential.password_hash == expected_hash)
    result = await db.execute(
        update(PasswordCredential)
        .where(*conditions)
        .values(password_hash=new_hash, updated_at=utcnow())
        .returning(PasswordCredential.id)
    )
    return result.first() is not None


async def mark_password_changed(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Set `users.password_changed_at` to now: a new credential version.

    The application clock, like the `iat` of the access tokens it is
    compared with (`User.password_changed_after`).
    """
    await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(password_changed_at=utcnow(), updated_at=func.now())
        .execution_options(synchronize_session=False)
    )


async def mark_password_changed_if_unchanged(
    db: AsyncSession, user_id: uuid.UUID, seen: datetime | None
) -> bool:
    """`mark_password_changed`, only if the version is still `seen`.

    Returns False, and changes nothing, if a password change or reset was
    committed since `seen` was read.
    """
    result = await db.execute(
        update(User)
        .where(
            User.id == user_id,
            User.password_changed_at.is_not_distinct_from(seen),
        )
        .values(password_changed_at=utcnow(), updated_at=func.now())
        .returning(User.id)
        .execution_options(synchronize_session=False)
    )
    return result.first() is not None


async def set_password_must_change(db: AsyncSession, user_id: uuid.UUID) -> bool:
    """Flag the user's password for a forced change.

    Returns False if the user has no password credential.
    """
    result = await db.execute(
        update(PasswordCredential)
        .where(PasswordCredential.user_id == user_id)
        .values(must_change=True, updated_at=utcnow())
        .returning(PasswordCredential.id)
    )
    return result.first() is not None


async def create_session(db: AsyncSession, session: Session) -> Session:
    """Add a refresh-token session to the session."""
    db.add(session)
    return session


class SessionOwner(NamedTuple):
    """Owner and family of a session, and whether the rotation gate would pass."""

    user_id: uuid.UUID
    family_id: uuid.UUID
    live: bool


async def get_session_owner(db: AsyncSession, token_hash: str) -> SessionOwner | None:
    """Return the owner and family of a session (any state) without locking.

    Both columns never change, so reading them before the user lock is safe.
    `live` uses the predicate and the database clock of `rotate_session`; it
    can turn false before the gate runs, never true.
    """
    live = and_(Session.is_revoked.is_(False), Session.expires_at > func.now())
    result = await db.execute(
        select(Session.user_id, Session.family_id, live).where(
            Session.token_hash == token_hash
        )
    )
    row = result.first()
    if row is None:
        return None
    return SessionOwner(row[0], row[1], bool(row[2]))


async def get_session_by_token_hash(
    db: AsyncSession, token_hash: str
) -> Session | None:
    """Return the session of a token hash in any state, or None."""
    result = await db.execute(select(Session).where(Session.token_hash == token_hash))
    return result.scalars().first()


async def rotate_session(db: AsyncSession, token_hash: str) -> datetime | None:
    """Atomically consume a refresh token: the single gate for rotation.

    `UPDATE ... WHERE NOT is_revoked AND expires_at > now() RETURNING`: of
    several concurrent calls with the same token exactly one gets a row.
    A session never expires after its family's end (a CHECK constraint), so
    the gate also refuses every token of an ended family. The consumed row
    drops its IP address, user agent and device: it stays only for reuse
    detection, which needs none of them, and the successor carries the
    current values. Returns the family's end (`family_expires_at`) or None
    if the token is unknown, revoked or expired.

    The caller holds the user lock (`lock_user`) already and writes the
    successor session in the same transaction, so a revocation or replay
    that queues on the user lock sees the successor.
    """
    result = await db.execute(
        update(Session)
        .where(
            Session.token_hash == token_hash,
            Session.is_revoked.is_(False),
            Session.expires_at > func.now(),
        )
        .values(
            is_revoked=True,
            revoke_reason="rotated",
            revoked_at=func.now(),
            ip_address=None,
            user_agent=None,
            device_id=None,
            device_name=None,
        )
        .returning(Session.family_expires_at)
    )
    return result.scalar_one_or_none()


async def revoke_user_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
    reason: str,
    except_family: uuid.UUID | None = None,
) -> int:
    """Revoke every active session of a user, optionally keeping one family.

    Active means not revoked and not expired. An expired session cannot be
    refreshed any more, and leaving it alone keeps its `revoked_at` empty,
    so the cleanup's retention still counts from its family's end. Reuse
    detection is unaffected: it looks only for sessions revoked as
    `rotated`.

    Takes the user lock first, so a refresh rotating a session of this user
    right now finishes before, and its successor is revoked as well.
    Returns the number of revoked sessions.
    """
    await lock_user(db, user_id)
    conditions = [
        Session.user_id == user_id,
        Session.is_revoked.is_(False),
        Session.expires_at > func.now(),
    ]
    if except_family is not None:
        conditions.append(Session.family_id != except_family)
    result = await db.execute(
        update(Session)
        .where(*conditions)
        .values(is_revoked=True, revoke_reason=reason, revoked_at=func.now())
        .returning(Session.id)
    )
    return len(result.all())


async def get_session_user_id(
    db: AsyncSession, session_id: uuid.UUID
) -> uuid.UUID | None:
    """Return the owner of a session (any state) without locking.

    `user_id` never changes, so reading it before the user lock is safe.
    """
    result = await db.execute(select(Session.user_id).where(Session.id == session_id))
    return result.scalar_one_or_none()


async def revoke_active_session(
    db: AsyncSession, session_id: uuid.UUID, reason: str
) -> bool:
    """Revoke one session if it is still active.

    A session revoked earlier keeps its original reason and time. The
    caller holds the user lock (`lock_user`). Returns True if the session
    was revoked by this call.
    """
    result = await db.execute(
        update(Session)
        .where(Session.id == session_id, Session.is_revoked.is_(False))
        .values(is_revoked=True, revoke_reason=reason, revoked_at=func.now())
        .returning(Session.id)
    )
    return result.first() is not None


async def revoke_family(
    db: AsyncSession,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    reason: str = "reuse_detected",
) -> int:
    """Revoke all active sessions in a rotation family.

    Takes the user lock first (same order as every other revocation).
    Returns the number of revoked sessions.
    """
    await lock_user(db, user_id)
    result = await db.execute(
        update(Session)
        .where(
            Session.user_id == user_id,
            Session.family_id == family_id,
            Session.is_revoked.is_(False),
        )
        .values(is_revoked=True, revoke_reason=reason, revoked_at=func.now())
        .returning(Session.id)
    )
    return len(result.all())


async def create_email_token(db: AsyncSession, token: EmailToken) -> EmailToken:
    """Add an email token to the session."""
    db.add(token)
    return token


def _created_after_password_change() -> ColumnElement[bool]:
    """Email-token condition: created after the owner's last password change.

    A token created before it (or at the same instant) is dead, also when
    the change did not see it to invalidate it. A plain sub-select on
    `users`: it takes no lock there, so the lock order is unchanged.
    """
    return not_(
        select(User.id)
        .where(
            User.id == EmailToken.user_id,
            User.password_changed_at >= EmailToken.created_at,
        )
        .exists()
    )


async def email_token_is_open(
    db: AsyncSession, token_hash: str, purpose: EmailTokenPurpose
) -> bool:
    """Tell whether a token of `purpose` with this hash is open, without locking.

    A pre-check only: the redemption (`consume_email_token`) decides.
    """
    result = await db.execute(
        select(EmailToken.id).where(
            EmailToken.token_hash == token_hash,
            EmailToken.purpose == purpose,
            EmailToken.used_at.is_(None),
            EmailToken.invalidated_at.is_(None),
            EmailToken.expires_at > func.now(),
            _created_after_password_change(),
        )
    )
    return result.first() is not None


async def lock_open_email_token(
    db: AsyncSession, token_id: uuid.UUID
) -> EmailToken | None:
    """Lock and return an email token if it is still open.

    Open means not used, not invalidated, not expired and created after
    the owner's last password change. The row lock
    (`FOR NO KEY UPDATE`, held until the transaction ends) makes a
    redemption or an invalidation of this token wait, so it sees the hash
    the caller sets; a token that a committed change closed meanwhile is
    not returned. `email_tokens` is first in the lock order.
    """
    result = await db.execute(
        select(EmailToken)
        .where(
            EmailToken.id == token_id,
            EmailToken.used_at.is_(None),
            EmailToken.invalidated_at.is_(None),
            EmailToken.expires_at > func.now(),
            _created_after_password_change(),
        )
        .with_for_update(key_share=True, of=EmailToken)
    )
    return result.scalars().first()


async def set_email_token_hash(
    db: AsyncSession, token_id: uuid.UUID, token_hash: str
) -> None:
    """Replace the stored hash of an email token."""
    await db.execute(
        update(EmailToken)
        .where(EmailToken.id == token_id)
        .values(token_hash=token_hash)
        .execution_options(synchronize_session=False)
    )


async def invalidate_email_token(db: AsyncSession, token_id: uuid.UUID) -> None:
    """Soft-invalidate one email token."""
    await db.execute(
        update(EmailToken)
        .where(EmailToken.id == token_id, EmailToken.invalidated_at.is_(None))
        .values(invalidated_at=utcnow())
        .execution_options(synchronize_session=False)
    )


async def consume_email_token(
    db: AsyncSession, token_hash: str, *purposes: EmailTokenPurpose
) -> tuple[uuid.UUID, uuid.UUID, EmailTokenPurpose, str | None] | None:
    """Atomically redeem a single-use email token of one of `purposes`.

    `UPDATE ... SET used_at = now() WHERE <still valid> RETURNING`: of
    several concurrent redemptions exactly one gets a row. Returns
    `(token_id, user_id, purpose, new_email)` or None if the token is
    unknown, used, invalidated, expired, of another purpose, or created
    before the owner's last password change. Such a token is refused, not
    marked: the condition cannot become true again.

    The caller commits it together with the effect of the token, so a
    failure before that rolls the redemption back.
    """
    result = await db.execute(
        update(EmailToken)
        .where(
            EmailToken.token_hash == token_hash,
            EmailToken.purpose.in_(purposes),
            EmailToken.used_at.is_(None),
            EmailToken.invalidated_at.is_(None),
            EmailToken.expires_at > func.now(),
            _created_after_password_change(),
        )
        .values(used_at=func.now())
        .returning(
            EmailToken.id, EmailToken.user_id, EmailToken.purpose, EmailToken.new_email
        )
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1], row[2], row[3]


async def set_email_token_previous_email(
    db: AsyncSession, token_id: uuid.UUID, previous_email: str
) -> None:
    """Store the address an email change replaces."""
    await db.execute(
        update(EmailToken)
        .where(EmailToken.id == token_id)
        .values(previous_email=previous_email)
        .execution_options(synchronize_session=False)
    )


async def get_email_token(db: AsyncSession, token_id: uuid.UUID) -> EmailToken | None:
    """Return an email token in any state, without locking it."""
    result = await db.execute(select(EmailToken).where(EmailToken.id == token_id))
    return result.scalars().first()


async def invalidate_tokens_for_user_purpose(
    db: AsyncSession,
    user_id: uuid.UUID,
    purpose: EmailTokenPurpose,
) -> None:
    """Soft-invalidate the user's open tokens of `purpose`."""
    await db.execute(
        update(EmailToken)
        .where(
            and_(
                EmailToken.user_id == user_id,
                EmailToken.purpose == purpose,
                EmailToken.used_at.is_(None),
                EmailToken.invalidated_at.is_(None),
            )
        )
        .values(invalidated_at=utcnow())
    )


async def invalidate_open_email_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Soft-invalidate every open email token of the user, of any purpose.

    Ends a pending verification, email change and password reset at once.
    `email_tokens` comes first in the lock order, so call this before the
    user lock.
    """
    await db.execute(
        update(EmailToken)
        .where(
            EmailToken.user_id == user_id,
            EmailToken.used_at.is_(None),
            EmailToken.invalidated_at.is_(None),
        )
        .values(invalidated_at=utcnow())
    )


async def create_audit_log(db: AsyncSession, log: AuthAuditLog) -> AuthAuditLog:
    """Add an audit entry to the session."""
    db.add(log)
    return log


async def audit_event_since(
    db: AsyncSession, user_id: uuid.UUID, event: AuditEventType, since: datetime
) -> bool:
    """Tell whether the user has an audit row of `event` newer than `since`."""
    result = await db.execute(
        select(AuthAuditLog.id)
        .where(
            AuthAuditLog.user_id == user_id,
            AuthAuditLog.event_type == event,
            AuthAuditLog.created_at > since,
        )
        .limit(1)
    )
    return result.first() is not None


# PostgreSQL's GREATEST ignores NULL arguments, so these are the latest of
# the row's timestamps: when it (or its family) ended, or a later
# revocation, use or invalidation. The first argument is NOT NULL, so the
# value is never NULL. The extra `... < cutoff` on the first argument is
# implied by it, but lets the planner use that column's index, oldest rows
# first.


async def delete_ended_sessions(
    db: AsyncSession, retention: timedelta, limit: int
) -> int:
    """Delete up to `limit` sessions whose family ended longer than `retention` ago.

    A session is kept until its family has ended (`family_expires_at`) and
    it was last changed (revoked) for `retention`. Until the family ends, a
    successor of a rotated token can still be refreshed, and a rotated
    token that comes back must be found to revoke the family (reuse
    detection); after the end, every token of the family is refused anyway.
    No session expires after its family, so this also covers `expires_at`.

    Rows locked by another transaction (a revocation, a second cleanup run)
    are skipped (`SKIP LOCKED`), so this never waits on a row lock and never
    joins a deadlock. Takes no user lock.

    Returns:
        The number of deleted sessions.
    """
    cutoff = func.now() - retention
    batch = (
        select(Session.id)
        .where(
            Session.family_expires_at < cutoff,
            func.greatest(Session.family_expires_at, Session.revoked_at) < cutoff,
        )
        .order_by(Session.family_expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(
        delete(Session)
        .where(Session.id.in_(batch.scalar_subquery()))
        .returning(Session.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.all())


async def delete_closed_email_tokens(
    db: AsyncSession, retention: timedelta, limit: int
) -> int:
    """Delete up to `limit` email tokens closed longer than `retention` ago.

    Closed means expired, and also last changed (used or invalidated)
    before the cutoff. Every token expires within a day of its request, so
    this removes used, invalidated and never redeemed tokens alike, together
    with their `new_email` and `previous_email`. Rows locked by another
    transaction are skipped (`SKIP LOCKED`).

    Returns:
        The number of deleted tokens.
    """
    cutoff = func.now() - retention
    batch = (
        select(EmailToken.id)
        .where(
            EmailToken.expires_at < cutoff,
            func.greatest(
                EmailToken.expires_at, EmailToken.used_at, EmailToken.invalidated_at
            )
            < cutoff,
        )
        .order_by(EmailToken.expires_at)
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    result = await db.execute(
        delete(EmailToken)
        .where(EmailToken.id.in_(batch.scalar_subquery()))
        .returning(EmailToken.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.all())
