# src/common/cache.py

"""
Cache management module.
This module provides Redis connection management and fastapi-cache2 integration.
All Redis-related setup lives here; other modules can build on the client
as needed.
"""

from typing import Optional
import redis.asyncio as redis  # type: ignore
from fastapi import Request

# from fastapi_cache import FastAPICache
# from fastapi_cache.backends.redis import RedisBackend
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
        await client.ping()
    except redis.RedisError as e:
        logger.error("cache.connect_failed", error=str(e))
        raise

    logger.info("cache.pool_init")
    return client


def init_fastapi_cache(client: redis.Redis) -> None:
    """Initialise fastapi-cache2 using the provided async Redis client."""
    # FastAPICache.init(
    #    RedisBackend(client),
    #    prefix=settings.CACHE_KEY_PREFIX,
    #    cache_status_header="X-Cache",
    # )
    logger.info(
        "cache.fastapi_cache_init",
        prefix=settings.CACHE_KEY_PREFIX,
    )
