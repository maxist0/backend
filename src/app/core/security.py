"""Security primitives: access tokens (JWT), password hashing, token hashing.

Access tokens are HS256 JWTs with the algorithm pinned in code. Passwords are
hashed with Argon2id in worker threads, bounded per event loop. Opaque tokens
(refresh, email) are stored only as SHA-256 hashes.
"""

import hashlib
import secrets
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Final

import anyio
import jwt
from anyio.lowlevel import RunVar
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class TokenData(BaseModel):
    """Decoded and validated JWT access-token payload."""

    user_id: str  # canonical UUID string (claim `sub`)
    username: str | None = None
    jti: str
    # Refresh-token family id (claim `sid`); None for tokens issued before it existed.
    session_id: str | None = None


# Pinned in code, never taken from configuration or from the token header
# (RFC 8725 section 3.1, OWASP JWT cheat sheet).
JWT_ALGORITHM: Final = "HS256"
# Claims every access token must carry; PyJWT rejects tokens without them.
_REQUIRED_CLAIMS: Final = ["exp", "iat", "nbf", "sub", "jti"]
_ACCESS_TOKEN_TYPE: Final = "access"


def create_access_token(
    username: str,
    user_id: str,
    session_id: str,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT access token.

    `session_id` is the refresh-token family id (claim `sid`). It stays
    stable across refresh rotation and identifies the caller's session.
    """
    if expires_delta is None:
        expires_delta = timedelta(minutes=get_settings().ACCESS_TOKEN_EXPIRE_MINUTES)

    now = datetime.now(UTC)
    to_encode = {
        "sub": user_id,
        "username": username,
        "sid": session_id,
        "jti": str(uuid.uuid4()),
        "type": _ACCESS_TOKEN_TYPE,
        "exp": now + expires_delta,
        "iat": now,
        "nbf": now,
    }

    return jwt.encode(
        to_encode,
        get_settings().JWT_SECRET_KEY.get_secret_value(),
        algorithm=JWT_ALGORITHM,
    )


def _parse_uuid(value: object) -> str | None:
    """Return the canonical string form of a UUID claim, or None if invalid."""
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def decode_token(token: str) -> TokenData | None:
    """
    Decode and validate a JWT access token.
    Returns TokenData if valid, None for any invalid, expired or malformed token.
    """
    try:
        payload = jwt.decode(
            token,
            get_settings().JWT_SECRET_KEY.get_secret_value(),
            algorithms=[JWT_ALGORITHM],
            options={"require": _REQUIRED_CLAIMS},
        )
    except jwt.ExpiredSignatureError:
        logger.info("jwt.token_expired")
        return None
    except jwt.PyJWTError as e:
        logger.warning("jwt.decode_error", error_type=type(e).__name__)
        return None

    if payload.get("type") != _ACCESS_TOKEN_TYPE:
        logger.warning("jwt.invalid_token", reason="wrong_type")
        return None

    user_id = _parse_uuid(payload.get("sub"))
    if user_id is None:
        logger.warning("jwt.invalid_token", reason="sub_not_uuid")
        return None

    jti = payload.get("jti")
    if not isinstance(jti, str) or not jti:
        logger.warning("jwt.invalid_token", reason="invalid_jti")
        return None

    # `sid` is optional: tokens issued before it existed stay valid until they
    # expire (ACCESS_TOKEN_EXPIRE_MINUTES). If present, it must be a UUID.
    raw_sid = payload.get("sid")
    session_id = None
    if raw_sid is not None:
        session_id = _parse_uuid(raw_sid)
        if session_id is None:
            logger.warning("jwt.invalid_token", reason="sid_not_uuid")
            return None

    username = payload.get("username")
    return TokenData(
        user_id=user_id,
        username=username if isinstance(username, str) else None,
        jti=jti,
        session_id=session_id,
    )


# One limiter per event loop (RunVar), like anyio's own default thread limiter.
_password_hash_limiter_var: RunVar[anyio.CapacityLimiter] = RunVar(
    "password_hash_limiter"
)


def _password_hash_limiter() -> anyio.CapacityLimiter:
    """Bound concurrent Argon2 work: each operation needs ARGON2_MEMORY_KIB."""
    try:
        return _password_hash_limiter_var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(get_settings().PASSWORD_HASH_CONCURRENCY)
        _password_hash_limiter_var.set(limiter)
        return limiter


_login_admission_limiter_var: RunVar[anyio.CapacityLimiter] = RunVar(
    "login_admission_limiter"
)


def login_admission_limiter() -> anyio.CapacityLimiter:
    """Bound concurrent login credential checks (lock re-check + verify).

    Same capacity as the Argon2 limiter: a login is admitted only when it can
    be evaluated, and it re-reads the lock state after the wait. Separate
    from the Argon2 limiter because `verify_password` takes that one
    itself (a task cannot hold two tokens of one limiter). Admission holders
    wait for Argon2 slots, never the other way round: no deadlock.
    """
    try:
        return _login_admission_limiter_var.get()
    except LookupError:
        limiter = anyio.CapacityLimiter(get_settings().PASSWORD_HASH_CONCURRENCY)
        _login_admission_limiter_var.set(limiter)
        return limiter


@lru_cache(maxsize=1)
def _password_hasher() -> PasswordHasher:
    """Argon2id hasher configured from Settings (created on first use)."""
    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.ARGON2_TIME_COST,
        memory_cost=settings.ARGON2_MEMORY_KIB,
        parallelism=settings.ARGON2_PARALLELISM,
    )


@lru_cache(maxsize=1)
def _dummy_password_hash() -> str:
    """Hash of a random password, used to equalise timing for unknown users."""
    return _password_hasher().hash(secrets.token_urlsafe(16))


class VerifyDurationAverage:
    """Moving average (EWMA) of real password-verify durations, in seconds.

    Every real verify of this process feeds it (`verify_password` and
    `equalize_password_timing`). The first sample replaces the cold-start
    value; later samples move the average by `weight`, so roughly the last
    ten verifies dominate and a single slow outlier does not.

    Args:
        cold_start_seconds: Value until the first verify was measured.
        weight: Share of each new sample in the average (0 < weight <= 1).
    """

    def __init__(self, cold_start_seconds: float, weight: float = 0.2) -> None:
        self.seconds = cold_start_seconds
        self._weight = weight
        self._measured = False

    def observe(self, seconds: float) -> None:
        """Add one measured verify duration."""
        if not self._measured:
            self.seconds = seconds
            self._measured = True
            return
        self.seconds += self._weight * (seconds - self.seconds)


# Cold start only (until the process has run its first verify): the order of
# magnitude of one verify with the default parameters (t=3, 64 MiB, p=4),
# measured at 40-60 ms on a developer machine on 2026-11-17.
_COLD_START_VERIFY_SECONDS: Final = 0.05
password_verify_seconds = VerifyDurationAverage(_COLD_START_VERIFY_SECONDS)


def _timed_verify_sync(password: str, password_hash: str) -> tuple[bool, float]:
    start = time.perf_counter()
    try:
        valid: bool = _password_hasher().verify(password_hash, password)
    except VerificationError, InvalidHashError:
        valid = False
    return valid, time.perf_counter() - start


def _burn_verify_sync(password: str) -> float:
    return _timed_verify_sync(password, _dummy_password_hash())[1]


async def hash_password(password: str) -> str:
    """Return an argon2id hash of *password* (computed in a worker thread)."""
    return await anyio.to_thread.run_sync(
        _password_hasher().hash, password, limiter=_password_hash_limiter()
    )


async def verify_password(password: str, password_hash: str) -> bool:
    """Return True if *password* matches *password_hash* (worker thread)."""
    valid, seconds = await anyio.to_thread.run_sync(
        _timed_verify_sync,
        password,
        password_hash,
        limiter=_password_hash_limiter(),
    )
    password_verify_seconds.observe(seconds)
    return valid


def password_needs_rehash(password_hash: str) -> bool:
    """True if *password_hash* was made with other parameters than configured."""
    return _password_hasher().check_needs_rehash(password_hash)


async def equalize_password_timing(password: str) -> None:
    """Spend one verify when no real verify happens (unknown/inactive user).

    Keeps the response time of those branches close to a wrong-password
    attempt, so timing does not reveal whether an account exists.
    """
    seconds = await anyio.to_thread.run_sync(
        _burn_verify_sync, password, limiter=_password_hash_limiter()
    )
    password_verify_seconds.observe(seconds)


async def wait_like_password_verify(
    sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> None:
    """Wait as long as a password verify takes, without computing one.

    For refusals that must not run Argon2 (a locked account: guesses are
    refused without an evaluation) but must not answer faster than a wrong
    password either. Call it outside the login admission limiter, so the
    wait holds no slot.

    Args:
        sleep: Async sleep function; tests inject a recorder.
    """
    await sleep(password_verify_seconds.seconds)


def hash_token(raw_token: str) -> str:
    """Return the SHA-256 hex digest of a raw token, for storage at rest.

    Args:
        raw_token: The token as handed to the client.

    Returns:
        The 64-character hex digest.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()
