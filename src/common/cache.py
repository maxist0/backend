#src/common/cache.py

"""
Cache management module.
This module provides functionality to initialize and manage cache connections.
"""

from typing import Optional
import redis.asyncio as redis
from fastapi import Request
from src.common.config import settings
from src.common.logging import get_logger

logger = get_logger(__name__)


async def get_cache_connection(request: Request) -> Optional[redis.Redis]:
    """Dependency to get a cache connection from the app state."""
    return request.app.state.cache

async def init_cache_pool() -> Optional[redis.Redis]:
    """Initialize the cache connection pool."""
    
    pool = redis.ConnectionPool(
        host=settings.CACHE_HOST,
        port=settings.CACHE_PORT,
        db=settings.CACHE_DB,
        password=settings.CACHE_PASSWORD,
        max_connections=settings.CACHE_MAX_CONNECTIONS,
    )
    # Check connection
    client = redis.Redis(connection_pool=pool)
    try:
        await client.ping()
    except redis.RedisError as e:
        # Handle connection error (log or raise)
        logger.error(f"Failed to connect to cache: {e}")
        raise e
    
    logger.info("Cache connection pool initialized.")
    return client


