# src/common/cache.py

"""
Cache management module.
This module provides Redis connection management and fastapi-cache2 integration.
All Redis-related setup lives here; other modules can build on the client
as needed.
"""

from typing import Optional
import redis.asyncio as redis  # type: ignore
from cashews import cache as route_cache
from fastapi import Request
from urllib.parse import quote_plus

from src.common.config import settings
from src.common.logging import get_logger

logger = get_logger(__name__)


async def get_cache_connection(request: Request) -> Optional[redis.Redis]:
    """Dependency to get the Redis client stored on app state."""
    return request.app.state.cache


async def init_cache_pool() -> redis.Redis:
    """Create and verify the async Redis connection pool, return the client."""
    pool = redis.ConnectionPool(
        host=settings.CACHE_HOST,
        port=settings.CACHE_PORT,
        db=settings.CACHE_DB,
        password=settings.CACHE_PASSWORD,
        max_connections=settings.CACHE_MAX_CONNECTIONS,
    )
    client = redis.Redis(connection_pool=pool)
    try:
        await client.ping()  # type: ignore[misc]
    except redis.RedisError as e:
        logger.error("cache.connect_failed", error=str(e))
        raise

    logger.info("cache.pool_init")
    return client


def init_fastapi_cache() -> None:
    """Initialise cashews cache used by route decorators and cache middlewares."""
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


async def close_fastapi_cache() -> None:
    """Close cashews cache backend on shutdown."""
    if not settings.ROUTE_CACHE_ENABLED:
        return
    await route_cache.close()
    logger.info("cache.route_cache_closed")
