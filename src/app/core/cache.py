"""Redis client (`RedisDep`) and the `@cached` / `invalidate` cache.

Nothing connects at import time. The lifespan opens the Redis client with
`open_redis` and configures the cache with `setup_cache`; both are closed on
shutdown. `cached` decorates at import and needs the setup only when the
decorated function runs.

The cache stores values as JSON. cashews would pickle them by default, and
unpickling data read from Redis runs code chosen by whoever can write to
Redis. A cached function therefore returns JSON types only (`dict`, `list`,
`str`, `int`, `float`, `bool`, `None`); cache `model.model_dump(mode="json")`
instead of a Pydantic model.
"""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import redis.asyncio as redis
from cashews import Cache
from cashews.picklers import PicklerType
from fastapi import Depends, Request

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# cashews binds decorators to a `Cache` object at import; the lifespan
# configures its backend.
_cache = Cache(name="app")


async def open_redis(settings: Settings) -> redis.Redis:
    """Create the async Redis client and check that the server answers.

    The client owns its connection pool; `close_redis` (`aclose`) also
    disconnects the pool. On a failed ping the client is closed before the
    error propagates.
    """
    client = redis.Redis(
        host=settings.CACHE_HOST,
        port=settings.CACHE_PORT,
        db=settings.CACHE_DB,
        password=settings.CACHE_PASSWORD,
        max_connections=settings.CACHE_MAX_CONNECTIONS,
    )
    try:
        await client.ping()  # pyright: ignore[reportUnknownMemberType] - redis-py leaves `**kwargs` of ping() unannotated
    except redis.RedisError as exc:
        await client.aclose()
        logger.error("cache.connect_failed", error_type=type(exc).__name__)
        raise
    logger.info("cache.redis_connected")
    return client


async def close_redis(client: redis.Redis) -> None:
    """Close the Redis client and its connection pool."""
    await client.aclose()
    logger.info("cache.redis_closed")


def get_redis(request: Request) -> redis.Redis:
    """Return the Redis client the lifespan stored on `app.state`."""
    client: redis.Redis = request.app.state.redis
    return client


RedisDep = Annotated[redis.Redis, Depends(get_redis)]


def setup_cache(settings: Settings) -> None:
    """Point the cache at the same Redis server and database as `RedisDep`.

    cashews builds its own connection pool from the URL (its Redis backend
    takes no client), bounded by `CACHE_MAX_CONNECTIONS`. Opens no connection.
    """
    _cache.setup(
        f"redis://{settings.CACHE_HOST}:{settings.CACHE_PORT}/{settings.CACHE_DB}",
        password=settings.CACHE_PASSWORD,
        max_connections=settings.CACHE_MAX_CONNECTIONS,
        pickle_type=PicklerType.JSON.value,
    )


async def close_cache() -> None:
    """Close the cache's connection pool."""
    await _cache.close()


def cached[**P, R](
    ttl: int | float | str | Callable[..., Any], key: str
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """Cache the return value of an async function in Redis.

    Args:
        ttl: Lifetime in seconds, or a string such as `"5m"`. A callable is
            called with the function's arguments on each store, so it can
            read the TTL from `get_settings()` at call time.
        key: Key template formatted with the function's arguments, for
            example `"profile:{user_id}"`. It must contain every argument
            that changes the result; a missing `{user_id}` serves one user's
            data to another.

    Returns:
        The decorator. The decorated function must return JSON types.
    """
    decorator: Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]] = (
        _cache(ttl=ttl, key=key)
    )
    return decorator


async def invalidate(pattern: str) -> None:
    """Delete every cached value whose key matches `pattern` (glob, `*`).

    Call it in the service after each mutation that changes cached data,
    for example `await invalidate(f"profile:{user_id}")`.
    """
    await _cache.delete_match(pattern)
