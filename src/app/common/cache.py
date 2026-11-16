# src/app/common/cache.py

"""
Cache management module.

Redis client and cashews route-cache setup. Nothing connects at import time:
the lifespan in main.py calls ``init_cache_pool()`` and ``init_route_cache()``
and closes both on shutdown.
"""

from urllib.parse import quote_plus

import redis.asyncio as redis
from cashews import cache as route_cache
from fastapi import Request

from app.common.config import Settings
from app.common.logging import get_logger

logger = get_logger(__name__)


async def get_cache_connection(request: Request) -> redis.Redis | None:
    """Dependency to get the Redis client stored on app state."""
    client: redis.Redis | None = getattr(request.app.state, "cache", None)
    return client


async def init_cache_pool(settings: Settings) -> redis.Redis:
    """Create and verify the async Redis client, return it.

    The client owns its connection pool, so ``close_cache_pool()`` (``aclose``)
    also disconnects the pool. On a failed ping the client is closed before
    the error propagates.
    """
    client = redis.Redis(
        host=settings.CACHE_HOST,
        port=settings.CACHE_PORT,
        db=settings.CACHE_DB,
        password=settings.CACHE_PASSWORD,
        max_connections=settings.CACHE_MAX_CONNECTIONS,
    )
    try:
        # redis-py >= 8 types ping() per client: Awaitable[bool] on asyncio.
        await client.ping()
    except redis.RedisError as e:
        await client.aclose()
        logger.error("cache.connect_failed", error_type=type(e).__name__)
        raise

    logger.info("cache.pool_init")
    return client


async def close_cache_pool(client: redis.Redis) -> None:
    """Close the Redis client and its connection pool."""
    await client.aclose()
    logger.info("cache.pool_closed")


def init_route_cache(settings: Settings) -> None:
    """Initialise the cashews cache used by route decorators and middlewares."""
    if not settings.ROUTE_CACHE_ENABLED:
        logger.info("cache.route_cache_disabled")
        return

    auth = ""
    if settings.CACHE_PASSWORD:
        auth = f":{quote_plus(settings.CACHE_PASSWORD)}@"
    cache_uri = (
        f"redis://{auth}{settings.CACHE_HOST}:{settings.CACHE_PORT}/{settings.CACHE_DB}"
    )
    route_cache.setup(cache_uri)
    logger.info(
        "cache.route_cache_init",
        route_cache_ttl=settings.ROUTE_CACHE_TTL,
    )


async def close_route_cache(settings: Settings) -> None:
    """Close the cashews cache backend on shutdown."""
    if not settings.ROUTE_CACHE_ENABLED:
        return
    await route_cache.close()
    logger.info("cache.route_cache_closed")
