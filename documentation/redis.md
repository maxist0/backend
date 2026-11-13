# Redis Caching

## Overview

Redis is used for **distributed caching** and **session storage** in this application. The project uses:

- **redis-py** (async library): Low-level Redis client
- **fastapi-cache2**: HTTP response caching decorator
- **Connection pooling**: Reuse connections efficiently

Redis enables:
- Fast data retrieval without database queries
- Session token storage and invalidation
- Rate limiting and throttling
- Real-time counters and analytics

## Architecture

```
FastAPI Application
    ↓
fastapi-cache2
    ↓ (cache hit)
Returns cached response
    ↓ (cache miss)
    ├→ Execute route handler
    ├→ Cache response
    └→ Return response
    ↓
Redis (in-memory store)
```

## Configuration

Redis settings in [src/common/config.py](../src/common/config.py):

```python
# Connection
CACHE_HOST=localhost
CACHE_PORT=6379
CACHE_DB=0                           # Redis logical database (0-15)
CACHE_PASSWORD=None                  # Set if Redis requires auth

# Connection Pool
CACHE_MAX_CONNECTIONS=10             # Max concurrent connections

# Naming
CACHE_KEY_PREFIX=fastapi-cache       # Prefix all keys with this
```

### Environment-Specific Configuration

```ini
# .env.dev
CACHE_HOST=localhost
CACHE_PORT=6379
CACHE_PASSWORD=

# .env.prod
CACHE_HOST=redis.prod.internal
CACHE_PORT=6379
CACHE_PASSWORD=secure-password-here
CACHE_MAX_CONNECTIONS=20             # Handle more concurrent requests
```

## Redis Client

Low-level Redis operations via `redis-py`:

```python
from src.common.cache import get_cache_connection
from fastapi import Request

@app.get("/data")
async def get_data(request: Request) -> dict:
    redis = await get_cache_connection(request)
    
    # Get value
    value = await redis.get("my_key")
    
    # Set value (with expiration)
    await redis.setex("my_key", 3600, "my_value")  # 1 hour
    
    # Check existence
    exists = await redis.exists("my_key")
    
    # Delete
    await redis.delete("my_key")
```

## fastapi-cache2 Decorator

High-level caching for route responses:

```python
from fastapi_cache.decorator import cache
from datetime import timedelta

@app.get("/users/{user_id}")
@cache(expire=timedelta(minutes=5))  # Cache for 5 minutes
async def get_user(user_id: UUID) -> UserResponse:
    # This response is cached
    user = await fetch_user(user_id)
    return UserResponse.from_orm(user)
```

### Cache Key Generation

Cache keys are auto-generated from:
- Function name
- Request parameters
- Query string parameters

**Example**:
```python
@cache(expire=300)  # 5 minutes
async def get_user(user_id: UUID, include_posts: bool = False):
    pass

# Generated keys:
# fastapi-cache:get_user(user_id=123, include_posts=False)
# fastapi-cache:get_user(user_id=456, include_posts=True)
```

### Custom Cache Keys

Specify custom key logic:

```python
from fastapi_cache.backends.redis import cache_key_builder

def custom_key_builder(
    func,
    namespace: str = "",
    request = None,
    response = None,
    *args,
    **kwargs,
):
    return f"custom:{func.__name__}:{kwargs.get('user_id')}"

@cache(expire=300, key_builder=custom_key_builder)
async def get_user(user_id: UUID) -> UserResponse:
    pass
```

### Cache Invalidation

```python
from fastapi_cache import FastAPICache

# Invalidate specific cache entry
await FastAPICache.clear(namespace="custom:get_user:123")

# Invalidate all caches with prefix
await FastAPICache.clear(namespace="fastapi-cache")
```

## Common Cache Patterns

### User Profile Caching

```python
@app.get("/users/{user_id}")
@cache(expire=timedelta(minutes=30))  # 30 min TTL
async def get_user(user_id: UUID, db: DbSession) -> UserResponse:
    user = await db.get(User, user_id)
    return UserResponse.from_orm(user)

@app.put("/users/{user_id}")
async def update_user(user_id: UUID, updates: UpdateUserRequest, db: DbSession):
    user = await db.get(User, user_id)
    # ... apply updates and save ...
    
    # Invalidate cache on update
    await FastAPICache.clear(namespace=f"fastapi-cache:get_user({user_id})")
    
    return UserResponse.from_orm(user)
```

### Expensive Query Caching

```python
@app.get("/reports/sales")
@cache(expire=timedelta(hours=1))  # 1 hour TTL for expensive report
async def get_sales_report(month: int, db: DbSession) -> SalesReport:
    # Expensive multi-table join
    report = await calculate_sales_by_month(month, db)
    return report
```

### Search Result Caching

```python
@app.get("/search")
@cache(expire=timedelta(minutes=10))  # 10 min TTL
async def search_items(
    q: str,
    page: int = 1,
    limit: int = 20,
    db: DbSession = Depends(get_db),
) -> list[ItemResponse]:
    # Cache per search query + pagination
    results = await db.execute(
        select(Item).where(
            Item.name.ilike(f"%{q}%")
        ).offset((page-1)*limit).limit(limit)
    )
    return [ItemResponse.from_orm(r) for r in results.scalars()]
```

### Cache Warming (Pre-population)

```python
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Warm cache
    redis = app.state.cache
    
    # Pre-fetch popular items
    await redis.setex(
        "popular_users:top10",
        3600,
        json.dumps([user.dict() for user in await get_top_10_users()])
    )
    
    yield
    
    # Shutdown
    await redis.close()
```

## Redis Data Types

### String

For simple key-value storage:

```python
# Set and get
await redis.set("feature_flag:new_ui", "true")
value = await redis.get("feature_flag:new_ui")

# Atomic counter
await redis.incr("api_requests_total")
await redis.decr("api_requests_total")
await redis.incrby("api_requests_total", 5)
```

### Hash

For object storage:

```python
# Store user session data
await redis.hset(
    f"session:{session_id}",
    mapping={
        "user_id": str(user_id),
        "username": "john",
        "ip_address": "192.168.1.1",
        "created_at": datetime.now().isoformat(),
    }
)

# Retrieve
user_id = await redis.hget(f"session:{session_id}", "user_id")
all_data = await redis.hgetall(f"session:{session_id}")
```

### Set

For unique collections:

```python
# Online users
await redis.sadd("online_users", user_id)
await redis.srem("online_users", user_id)

# Check membership
is_online = await redis.sismember("online_users", user_id)

# Get all
online = await redis.smembers("online_users")
```

### List

For ordered queues:

```python
# Queue operations
await redis.rpush("email_queue", json.dumps({"to": "user@example.com"}))
email = await redis.lpop("email_queue")

# Get range
first_10 = await redis.lrange("email_queue", 0, 9)
```

### Sorted Set

For ranked data:

```python
# Leaderboard
await redis.zadd("user_scores", {user_id: score})

# Top 10
top_10 = await redis.zrevrange("user_scores", 0, 9, withscores=True)

# User rank
rank = await redis.zrevrank("user_scores", user_id)
```

## Key Naming Conventions

Use hierarchical, descriptive keys:

```
# User data
user:{user_id}:profile          # User profile object
user:{user_id}:settings         # User settings
user:{user_id}:sessions         # Set of session IDs

# Session data
session:{session_id}            # Session object
session:{session_id}:data       # Session data

# Counters
counter:api_requests_total      # Total requests
counter:api_errors              # Total errors
counter:users_online            # Online users

# Temporary data
temp:{request_id}:upload_status # Upload progress

# Lists/Queues
queue:email:pending             # Email queue
queue:tasks:background          # Background job queue

# Sets (unique collections)
set:online_users                # Online user IDs
set:admin_users                 # Admin user IDs

# Sorted sets (leaderboards)
leaderboard:daily_scores        # Daily top users
leaderboard:monthly_scores      # Monthly top users
```

## Expiration and TTL

Set expiration on all keys:

```python
# 1 hour expiration
await redis.setex("key", 3600, "value")

# 30 days expiration
await redis.setex("key", 30*24*3600, "value")

# Get remaining TTL
ttl = await redis.ttl("key")  # Returns seconds, -1 if no expiration

# Update expiration on existing key
await redis.expire("key", 3600)

# Remove expiration
await redis.persist("key")
```

### TTL Guidelines

| Data Type | TTL | Rationale |
|-----------|-----|-----------|
| User profile | 1 hour | Changes infrequently |
| User settings | 24 hours | Rarely change |
| API responses | 5-15 min | Need freshness |
| Session data | Until logout | Session length |
| Rate limit counters | 1 minute | Per-minute limits |
| Feature flags | No expiration | Rarely change |
| Temporary data | 5 minutes | Clean up quickly |

## Monitoring and Debugging

### Redis Connection Status

```python
# In route handler
redis = await get_cache_connection(request)
info = await redis.ping()  # Returns True if connected
```

### Cache Statistics

```python
redis = request.app.state.cache

# Get Redis info
info = await redis.info()
print(f"Memory used: {info['used_memory_human']}")
print(f"Connected clients: {info['connected_clients']}")
print(f"Keyspace: {info['db0']['keys']}")
```

### View All Keys (Development Only)

```python
# Get all keys with prefix (development debugging)
keys = await redis.keys("fastapi-cache:*")
for key in keys:
    value = await redis.get(key)
    logger.debug("cache_entry", key=key, value=value)
```

### Clear All Cache (Development)

```python
# Flush entire database (use with caution!)
await redis.flushdb()

# Flush all databases (use with extreme caution!)
await redis.flushall()
```

## Error Handling

### Connection Errors

```python
from redis.exceptions import RedisError

@app.get("/data")
async def get_data(request: Request) -> dict:
    try:
        redis = await get_cache_connection(request)
        cached = await redis.get("key")
        if cached:
            return json.loads(cached)
    except RedisError as e:
        logger.error("cache.connection_failed", error=str(e))
        # Fall back to database query
    
    return await get_data_from_db()
```

### Graceful Degradation

If Redis is unavailable, the application continues functioning (without caching):

```python
# Typical pattern with fallback
async def get_user(user_id: UUID, request: Request, db: DbSession):
    try:
        redis = await get_cache_connection(request)
        cached = await redis.get(f"user:{user_id}")
        if cached:
            return json.loads(cached)
    except RedisError:
        logger.warning("cache.unavailable")
        # Continue without cache
    
    # Query database
    user = await db.get(User, user_id)
    
    # Try to cache
    try:
        redis = await get_cache_connection(request)
        await redis.setex(f"user:{user_id}", 3600, user.json())
    except RedisError:
        pass  # Caching failed, but continue
    
    return user
```

## Redis in Docker

### Docker Compose

```yaml
services:
  redis:
    image: redis:latest
    container_name: redis
    ports:
      - "6379:6379"
    command: redis-server --appendonly yes  # AOF persistence
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
    volumes:
      - redis_data:/data
    restart: always

volumes:
  redis_data:
```

### Kubernetes

```yaml
apiVersion: v1
kind: Service
metadata:
  name: redis-primary
spec:
  ports:
    - port: 6379
  selector:
    app: redis
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: redis-primary
spec:
  serviceName: redis-primary
  replicas: 1
  selector:
    matchLabels:
      app: redis
  template:
    metadata:
      labels:
        app: redis
    spec:
      containers:
      - name: redis
        image: redis:latest
        ports:
        - containerPort: 6379
        command:
          - redis-server
          - "--appendonly"
          - "yes"
```

---

**Related Documentation:**
- See [environment.md](environment.md) for environment-specific configuration
- See [monitoring.md](monitoring.md) for cache performance monitoring
