"""Data-access layer of the auth module.

Credentials, sessions, email tokens and the audit log, plus the lockout
counters and the per-user lock on `users`. PostgreSQL is the single source
of truth for sessions (refresh tokens); nothing is cached in Redis.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, not_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import get_logger, utcnow
from app.modules.auth.models import (
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

    Lock order everywhere: `email_tokens` -> `users` -> `sessions`.
    `NO KEY UPDATE` (not `UPDATE`) leaves the `KEY SHARE` locks of
    foreign-key checks free: inserts into sessions or auth_audit_log for
    this user do not wait. Does not commit. No-op for an unknown user.
    """
    await db.execute(
        select(User.id).where(User.id == user_id).with_for_update(key_share=True)
    )


async def lock_and_get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    """`lock_user` that also returns the current row (fresh, not cached).

    `populate_existing` overwrites an instance already in the session's
    identity map with the values read under the lock. Does not commit.
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
    """Read `(is_locked, locked_until)` from the database, bypassing the
    identity map (a cached User would show the state of the first read)."""
    result = await db.execute(
        select(User.is_locked, User.locked_until).where(User.id == user_id)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def record_successful_login(
    db: AsyncSession, user_id: uuid.UUID
) -> datetime | None:
    """Reset the failure counter, but only if no lock is active right now.

    One conditional `UPDATE ... RETURNING`: a lock committed by a parallel
    wrong guess after this request checked the lock still wins. An expired
    timed lock is cleared. Returns the new `last_login_at`, or None when
    the account is locked (or gone). Commits.
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
            last_login_at=func.now(),
            updated_at=func.now(),
        )
        .returning(User.last_login_at)
        .execution_options(synchronize_session=False)
    )
    row = result.first()
    await db.commit()
    return row[0] if row is not None else None


async def register_failed_login(
    db: AsyncSession, user_id: uuid.UUID, max_attempts: int, lock_seconds: int
) -> tuple[int, bool, datetime | None] | None:
    """Count a failed login atomically and lock the account at the threshold.

    One `UPDATE ... RETURNING`: the increment happens in the database, so
    parallel failures cannot overwrite each other (no lost update). A timed
    lock (`locked_until`) is only set when the account is not locked yet,
    so an indefinite admin lock (`locked_until IS NULL`) is never weakened.
    Returns `(failed_login_attempts, is_locked, locked_until)` or None if
    the user does not exist. Commits.
    """
    reaches_threshold = User.failed_login_attempts + 1 >= max_attempts
    result = await db.execute(
        update(User)
        .where(User.id == user_id)
        .values(
            failed_login_attempts=User.failed_login_attempts + 1,
            is_locked=case((reaches_threshold, True), else_=User.is_locked),
            locked_until=case(
                (
                    and_(reaches_threshold, User.is_locked.is_(False)),
                    func.now() + timedelta(seconds=lock_seconds),
                ),
                else_=User.locked_until,
            ),
            updated_at=func.now(),
        )
        .returning(User.failed_login_attempts, User.is_locked, User.locked_until)
    )
    row = result.first()
    await db.commit()
    if row is None:
        return None
    return row[0], row[1], row[2]


async def create_password_credential(
    db: AsyncSession, credential: PasswordCredential
) -> PasswordCredential:
    db.add(credential)
    await db.commit()
    return credential


async def get_password_credential(
    db: AsyncSession, user_id: uuid.UUID
) -> PasswordCredential | None:
    result = await db.execute(
        select(PasswordCredential).where(PasswordCredential.user_id == user_id)
    )
    return result.scalars().first()


async def update_password_hash(
    db: AsyncSession, user_id: uuid.UUID, new_hash: str
) -> None:
    await db.execute(
        update(PasswordCredential)
        .where(PasswordCredential.user_id == user_id)
        .values(password_hash=new_hash, updated_at=utcnow())
    )
    await db.commit()


async def create_session(db: AsyncSession, session: Session) -> Session:
    db.add(session)
    await db.commit()
    return session


async def get_session_owner(
    db: AsyncSession, token_hash: str
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Return `(user_id, family_id)` of a session (any state) without locking.

    Both columns never change, so reading them before the user lock is safe.
    """
    result = await db.execute(
        select(Session.user_id, Session.family_id).where(
            Session.token_hash == token_hash
        )
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def get_session_by_token_hash(
    db: AsyncSession, token_hash: str
) -> Session | None:
    result = await db.execute(select(Session).where(Session.token_hash == token_hash))
    return result.scalars().first()


async def revoke_session(
    db: AsyncSession, session_id: uuid.UUID, reason: str = "logout"
) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(is_revoked=True, revoke_reason=reason, revoked_at=utcnow())
    )
    await db.commit()


async def rotate_session(
    db: AsyncSession, token_hash: str
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Atomically consume a refresh token: the single gate for rotation.

    `UPDATE ... WHERE NOT is_revoked AND expires_at > now() RETURNING`: of
    several concurrent calls with the same token exactly one gets a row.
    Returns `(user_id, family_id)` or None if the token is unknown, revoked
    or expired.

    The caller holds the user lock (`lock_user`) already. Does NOT commit:
    the caller writes the successor session in the same transaction, so a
    revocation or replay that queues on the user lock sees the successor.
    """
    result = await db.execute(
        update(Session)
        .where(
            Session.token_hash == token_hash,
            Session.is_revoked.is_(False),
            Session.expires_at > func.now(),
        )
        .values(is_revoked=True, revoke_reason="rotated", revoked_at=func.now())
        .returning(Session.user_id, Session.family_id)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1]


async def revoke_user_sessions(
    db: AsyncSession,
    user_id: uuid.UUID,
    reason: str,
    except_family: uuid.UUID | None = None,
) -> int:
    """Revoke every active session of a user, optionally keeping one family.

    Takes the user lock first, so a refresh rotating a session of this user
    right now finishes before, and its successor is revoked as well.
    Returns the number of revoked sessions. Commits.
    """
    await lock_user(db, user_id)
    conditions = [Session.user_id == user_id, Session.is_revoked.is_(False)]
    if except_family is not None:
        conditions.append(Session.family_id != except_family)
    result = await db.execute(
        update(Session)
        .where(*conditions)
        .values(is_revoked=True, revoke_reason=reason, revoked_at=func.now())
        .returning(Session.id)
    )
    revoked = len(result.all())
    await db.commit()
    return revoked


async def revoke_family(
    db: AsyncSession,
    user_id: uuid.UUID,
    family_id: uuid.UUID,
    reason: str = "reuse_detected",
) -> int:
    """Revoke all active sessions in a rotation family.

    Takes the user lock first (same order as every other revocation).
    Returns the number of revoked sessions. Always commits, also when no
    row was active, so earlier writes of the caller's transaction persist.
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
    revoked = len(result.all())
    await db.commit()
    return revoked


async def create_email_token(db: AsyncSession, token: EmailToken) -> EmailToken:
    db.add(token)
    await db.commit()
    return token


async def consume_email_token(
    db: AsyncSession, token_hash: str, *purposes: EmailTokenPurpose
) -> tuple[uuid.UUID, EmailTokenPurpose, str | None] | None:
    """Atomically redeem a single-use email token of one of `purposes`.

    `UPDATE ... SET used_at = now() WHERE <still valid> RETURNING`: of
    several concurrent redemptions exactly one gets a row. Returns
    `(user_id, purpose, new_email)` or None if the token is unknown, used,
    invalidated, expired or of another purpose.

    Does NOT commit: the caller's next write commits it together with the
    effect of the token (a failure before that rolls the redemption back).
    """
    result = await db.execute(
        update(EmailToken)
        .where(
            EmailToken.token_hash == token_hash,
            EmailToken.purpose.in_(purposes),
            EmailToken.used_at.is_(None),
            EmailToken.invalidated_at.is_(None),
            EmailToken.expires_at > func.now(),
        )
        .values(used_at=func.now())
        .returning(EmailToken.user_id, EmailToken.purpose, EmailToken.new_email)
    )
    row = result.first()
    if row is None:
        return None
    return row[0], row[1], row[2]


async def invalidate_tokens_for_user_purpose(
    db: AsyncSession,
    user_id: uuid.UUID,
    purpose: EmailTokenPurpose,
    *,
    commit: bool = True,
) -> None:
    """Soft-invalidate the user's open tokens of `purpose`.

    With `commit=False` the caller's next commit includes the invalidation.
    """
    await db.execute(
        update(EmailToken)
        .where(
            and_(
                EmailToken.user_id == user_id,
                EmailToken.purpose == purpose,
                EmailToken.used_at == None,  # noqa: E711
                EmailToken.invalidated_at == None,  # noqa: E711
            )
        )
        .values(invalidated_at=utcnow())
    )
    if commit:
        await db.commit()


async def create_audit_log(db: AsyncSession, log: AuthAuditLog) -> AuthAuditLog:
    db.add(log)
    await db.commit()
    return log
