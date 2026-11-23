"""Rate limits on Redis: `rate_limit` for routes, `hit` for services.

A policy has a fixed name (`auth.login.ip`), a limit (`"10/15 minutes"`)
and a key: the identifier it counts, such as the client address, a user id
or a normalised email address. Every request with the same key adds one
entry to a sliding-window log in Redis (cashews `slice_incr`, one atomic Lua
script); once the window holds more entries than the limit allows, the
request is refused with `RateLimitedError` (429 with `Retry-After`). A
refused request is recorded too, so a client that keeps sending stays
refused until it pauses.

Redis keys hold no identifier in clear text: the key is
`rate_limit:<policy>:<digest>`, where the digest is an HMAC-SHA256 of the
policy and the identifier. Its key is derived from `JWT_SECRET_KEY` with a
fixed label, so whoever can read Redis cannot test guesses (all IPv4
addresses, a list of email addresses) against the keys.

When the store fails (any Redis error: unreachable, timeout, out of memory,
read-only), a check lets the request through (fail-open), and the checks
of the next few seconds skip the store, so an outage adds no latency to
every request. `rate_limit_fail_open_total` counts each such request, and
`rate_limit.fail_open` is logged at most once a minute per process.
`RATE_LIMIT_ENABLED=false` turns every check off.

Only the API process can check limits: the API lifespan sets up the store.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import re
import time
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

from cashews.exceptions import CacheBackendInteractionError
from fastapi import Depends, Request, params
from prometheus_client import Counter

from app.core.cache import rate_limit_store
from app.core.config import Environment, Settings, get_settings
from app.core.errors import RateLimitedError
from app.core.logging import get_logger

logger = get_logger(__name__)

REJECTIONS = Counter(
    "rate_limit_rejections_total",
    "Requests refused with 429 by a rate limit, by policy.",
    ["policy"],
)
FAIL_OPEN = Counter(
    "rate_limit_fail_open_total",
    "Requests let through unchecked because Redis could not be reached, by policy.",
    ["policy"],
)

# Lower-case dotted names, at least `<module>.<what>`: a policy name is a
# metric label and part of a Redis key, so it must come from code, not input.
_POLICY = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
_POLICY_MAX_LENGTH = 64
_LIMIT = re.compile(r"([1-9][0-9]*)/(?:([1-9][0-9]*) )?(second|minute|hour|day)s?")
_UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3_600, "day": 86_400}

# Domain separation: the HMAC key for rate-limit identifiers is derived from
# JWT_SECRET_KEY under this label and is used for nothing else. A JWT
# signature is an HMAC over `<header>.<payload>`, which this label cannot be.
_KEY_LABEL = b"rate-limit identifier key v1"
# 128 bits of the HMAC: collisions between identifiers stay negligible.
_DIGEST_HEX_LENGTH = 32
# A client changes its IPv6 address freely inside its /64 (RFC 8981), so
# counting single IPv6 addresses would not limit anyone.
_IPV6_PREFIX = 64
# A /48 is a whole site's allocation (RIPE-690): whoever holds one can use
# 65,536 different /64s, so the coarser policies count the /48.
_IPV6_SITE_PREFIX = 48

# Seconds between two `rate_limit.fail_open` log lines of one process.
_OUTAGE_LOG_INTERVAL = 60.0
# Seconds the store is skipped after an error. A failing call can take up
# to about a second (connect or socket timeout), so without the skip every
# request would pay it during an outage; with it, one request per process
# pays it every 5 s. The limits stay off for at most that long after Redis
# is back.
_OUTAGE_SKIP_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class Rate:
    """A parsed limit: at most `limit` requests per `window` seconds."""

    limit: int
    window: int


@lru_cache(maxsize=256)
def parse_rate(text: str) -> Rate:
    """Parse a limit such as `"20/minute"`, `"10/15 minutes"` or `"3/hour"`.

    Raises:
        ValueError: The text is not `<count>/[<number> ]<unit>` with a unit
            of `second`, `minute`, `hour` or `day` (optionally plural).
    """
    match = _LIMIT.fullmatch(text)
    if match is None:
        raise ValueError(
            f"Rate limit {text!r} is not '<count>/[<number> ]<unit>', "
            "for example '20/minute' or '10/15 minutes'."
        )
    count, multiplier, unit = match.groups()
    return Rate(int(count), int(multiplier or 1) * _UNIT_SECONDS[unit])


@lru_cache(maxsize=256)
def _checked_policy(name: str) -> str:
    if len(name) > _POLICY_MAX_LENGTH or not _POLICY.fullmatch(name):
        raise ValueError(
            f"Rate-limit policy {name!r} must be a lower-case dotted name such "
            f"as 'notes.create.user', at most {_POLICY_MAX_LENGTH} characters."
        )
    return name


def client_address(host: str) -> str:
    """Normalise a client address into the identifier a per-IP policy counts.

    IPv4 addresses (also IPv4-mapped IPv6 ones) count one by one, IPv6
    addresses per /64 network. A value that is not an IP address, such as
    the host of a test client, is used as it is.
    """
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        # The integer drops a zone index (`%eth0`), which is no network.
        network = ipaddress.IPv6Network((int(address), _IPV6_PREFIX), strict=False)
        return str(network)
    return str(address)


def by_ip(request: Request) -> str:
    """Rate-limit key of a route: the client's address.

    Only the ASGI scope is read. uvicorn puts the forwarded client address
    there for proxies listed in `FORWARDED_ALLOW_IPS`; the API never parses
    `X-Forwarded-For` itself. Without a client address (a Unix socket
    without a proxy) all such requests share one key, which limits them
    together rather than not at all.
    """
    return client_address(request.client.host if request.client else "")


def by_ipv6_48(request: Request) -> str | None:
    """Rate-limit key of a route: the client's IPv6 /48 network.

    For a coarser second policy next to `by_ip`: it bounds a client that
    rotates through the /64s of its site. IPv4 clients (also IPv4-mapped
    ones) return None and are not counted by it; their per-address policy
    already limits them.
    """
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv6Address) or address.ipv4_mapped:
        return None
    network = ipaddress.IPv6Network((int(address), _IPV6_SITE_PREFIX), strict=False)
    return str(network)


def _redis_key(policy: str, identifier: str, secret: str) -> str:
    derived = hmac.new(secret.encode(), _KEY_LABEL, hashlib.sha256).digest()
    message = f"{policy}\0{identifier}".encode(errors="surrogatepass")
    digest = hmac.new(derived, message, hashlib.sha256).hexdigest()
    return f"rate_limit:{policy}:{digest[:_DIGEST_HEX_LENGTH]}"


def _retry_after(ttl: int, window: int) -> int:
    """Seconds until the policy's log is empty, never too early.

    The log key expires one window after its newest entry, so its TTL is
    when every entry has left the window. Redis rounds the TTL to the
    nearest second, hence one more second; the wait is never longer than
    the window. A negative TTL means the key is gone already.
    """
    return min(max(ttl, 0) + 1, window)


class StoreOutage:
    """Process-local state of a failing rate-limit store.

    After a store error the checks skip the store for `skip` seconds and let
    their requests through at once; the first check after that tries the
    store again. `rate_limit.fail_open` is logged at most once per
    `log_interval`, with the number of fail-open checks since the last line.

    Args:
        skip: Seconds the store is skipped after an error.
        log_interval: Seconds between two log lines.
        clock: Monotonic clock in seconds.
    """

    def __init__(
        self,
        *,
        skip: float,
        log_interval: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._skip = skip
        self._log_interval = log_interval
        self._clock = clock
        self._skip_until: float | None = None
        self._last_logged: float | None = None
        self._suppressed = 0

    def skipping(self) -> bool:
        """Whether checks skip the store now; a skipped check is counted."""
        if self._skip_until is None or self._clock() >= self._skip_until:
            return False
        self._suppressed += 1
        return True

    def failed(self, policy: str, exc: CacheBackendInteractionError) -> None:
        """Record a store error: skip the store and log unless logged recently."""
        now = self._clock()
        self._skip_until = now + self._skip
        if (
            self._last_logged is not None
            and now - self._last_logged < self._log_interval
        ):
            self._suppressed += 1
            return
        # The cause is redis-py's error; its message holds only the address.
        cause = exc.__cause__ or exc
        logger.warning(
            "rate_limit.fail_open",
            policy=policy,
            error_type=type(cause).__name__,
            suppressed=self._suppressed,
        )
        self._last_logged = now
        self._suppressed = 0


# Per process, like the metrics; only the event loop touches it.
store_outage = StoreOutage(skip=_OUTAGE_SKIP_SECONDS, log_interval=_OUTAGE_LOG_INTERVAL)


# One lock per key and process while a hit takes its timestamp and runs the
# log script. The script counts only entries up to the hit's own timestamp,
# so two hits that took their timestamps in one order and reached Redis in
# the other would each miss the other's entry, and a parallel burst would
# pass the limit. Across processes the same can still happen within clock
# skew and network jitter (README known issue 34). Weak values: a lock
# disappears once no hit holds or waits for it.
_key_locks: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)


def _key_lock(redis_key: str) -> asyncio.Lock:
    lock = _key_locks.get(redis_key)
    if lock is None:
        lock = asyncio.Lock()
        _key_locks[redis_key] = lock
    return lock


def warn_if_disabled(settings: Settings) -> None:
    """Log a warning at startup when the limits are off outside dev and tests."""
    if not settings.RATE_LIMIT_ENABLED and settings.ENVIRONMENT not in (
        Environment.DEV,
        Environment.UNITTEST,
    ):
        logger.warning("rate_limit.disabled", environment=settings.ENVIRONMENT.value)


async def hit(policy: str, limit: str, *, key: str) -> None:
    """Count one request against a rate limit; refuse it above the limit.

    For limits keyed by request data (an email address, a submitted
    username, a session family) inside a service or dependency. Call it
    before any work the limit protects, such as a database lookup or a
    password hash. Routes keyed by the client or the user use `rate_limit`.

    Only in the API process: the API lifespan sets up the store
    (`setup_cache`). Elsewhere, in a worker task for example, it raises
    cashews' `NotConfiguredError`.

    Args:
        policy: Fixed policy name, `<module>.<action>.<key kind>`, for
            example `auth.password_forgot.email`. Unique per limit: two
            limits with the same name share one counter.
        limit: For example `"3/hour"` or `"10/15 minutes"`.
        key: The identifier to count, normalised by the caller (an email
            address in lower case, as the account lookup compares it). It is
            stored only as an HMAC.

    Raises:
        RateLimitedError: The limit is exhausted; `Retry-After` holds the
            seconds until the window is free again (the whole window if
            Redis failed while reading it).
        ValueError: `policy` or `limit` is malformed (a programming error).
    """
    rate = parse_rate(limit)
    _checked_policy(policy)
    settings = get_settings()
    if not settings.RATE_LIMIT_ENABLED:
        return
    redis_key = _redis_key(policy, key, settings.JWT_SECRET_KEY.get_secret_value())
    async with _key_lock(redis_key):
        if store_outage.skipping():
            FAIL_OPEN.labels(policy=policy).inc()
            return
        now = time.time()
        try:
            # A limit of n allows n + 1 entries in the log: the extra one
            # marks a refusal and makes the count tell refused from accepted.
            count = await rate_limit_store.slice_incr(
                redis_key,
                now - rate.window,
                now,
                maxvalue=rate.limit + 1,
                expire=rate.window,
            )
        except CacheBackendInteractionError as exc:
            FAIL_OPEN.labels(policy=policy).inc()
            store_outage.failed(policy, exc)
            return
    if count <= rate.limit:
        return
    REJECTIONS.labels(policy=policy).inc()
    try:
        ttl = await rate_limit_store.get_expire(redis_key)
    except CacheBackendInteractionError as exc:
        # The refusal is already decided; the whole window is never too early.
        store_outage.failed(policy, exc)
        raise RateLimitedError(retry_after=rate.window) from None
    raise RateLimitedError(retry_after=_retry_after(ttl, rate.window))


type RateLimitKey = Callable[..., str | None] | Callable[..., Awaitable[str | None]]


def rate_limit(policy: str, limit: str, *, key: RateLimitKey) -> params.Depends:
    """A route dependency that counts each request against a rate limit.

    Usage:
        @router.post(
            "",
            dependencies=[rate_limit("notes.create.user", "30/minute", key=by_user)],
            responses=problem_responses(RateLimitedError, ...),
        )

    The limit and the name are checked when the route is defined. The
    route lists `RateLimitedError` in its `responses`.

    Args:
        policy: Fixed policy name, see `hit`.
        limit: For example `"20/minute"`.
        key: A FastAPI dependency that returns the identifier to count:
            `by_ip` (the client address), `by_ipv6_48` (the client's IPv6
            /48, None for IPv4) or `by_user` from `app.modules.users` (the
            signed-in user). A request whose key is None is not counted.
            Its own dependencies are resolved as usual, and cached for the
            request.

    Returns:
        The dependency for the route's `dependencies=[...]`.
    """
    parse_rate(limit)
    _checked_policy(policy)
    # Export both series from the start, so the first increment is a rate.
    REJECTIONS.labels(policy=policy)
    FAIL_OPEN.labels(policy=policy)

    async def check_rate_limit(
        identifier: Annotated[str | None, Depends(key)],
    ) -> None:
        if identifier is not None:
            await hit(policy, limit, key=identifier)

    dependency: params.Depends = Depends(check_rate_limit)
    return dependency
