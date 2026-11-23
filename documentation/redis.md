# Redis: cache and background jobs

What the application stores in Redis, how the cache works and how background jobs are delivered, retried and scheduled. Form: explanation with reference tables. Refresh tokens and sessions are **not** in Redis; they live only in Postgres ([authentication.md](authentication.md)).

## What lives where

One Redis server, three databases:

| Database | Settings | Holds |
|---|---|---|
| db 0 | `CACHE_HOST`, `CACHE_PORT`, `CACHE_DB` (default `0`), `CACHE_PASSWORD` | The `@cached` values, the rate-limit logs (`rate_limit:<policy>:<hmac>`, sorted sets of timestamps that expire one window after their newest entry; [ADR 0013](adr/0013-rate-limiting-in-the-app-on-redis.md)) and anything you store through `RedisDep`. The `rate_limit:` prefix is reserved |
| db 1 | `TASKIQ_BROKER_URL` | The task stream `taskiq:tasks` (consumer group `taskiq`) and the retry schedules (keys `taskiq_retry:*`) |
| db 2 | `TASKIQ_RESULT_BACKEND_URL` | Task results, JSON, 1 hour TTL |

The broker's Redis must not evict keys (`maxmemory-policy noeviction`): an evicted stream entry is a lost task. `compose.dev.yaml` runs `redis:7.4` without append-only persistence; it saves an RDB snapshot so a restart does not drop queued tasks.

## Cache

`@cached(ttl, key)` and `invalidate(pattern)` from `app.core` cache the return value of an async function ([ADR 0003](adr/0003-caching-via-cached-decorator.md)). The implementation is a thin wrapper over cashews (`src/app/core/cache.py`).

```python
from app.core import cached, invalidate


@cached(ttl=300, key="profile:{user_id}")
async def load_profile(user_id: str) -> dict[str, str]: ...


async def rename(user_id: str) -> None:
    ...  # write the change, commit
    await invalidate(f"profile:{user_id}")
```

- Values are stored as **JSON**, never pickled: a value read from Redis cannot run code. A cached function returns JSON types only (`dict`, `list`, `str`, `int`, `float`, `bool`, `None`); cache `model.model_dump(mode="json")`, not a Pydantic model.
- The `key` is a template formatted with the function's arguments and must hold every argument that changes the result. A missing `{user_id}` serves one user's data to another.
- `ttl` is seconds, or a string such as `"5m"`, or a callable that reads the TTL from `get_settings()` at call time. TTLs come from `Settings`, not from literals ([AGENTS.md](../AGENTS.md#caching-and-background-jobs)).
- A mutation calls `invalidate(pattern)` (a glob, `*`) in the service.
- There is no HTTP response cache and no ETag middleware.
- No built-in route uses `@cached` today.

`RedisDep` (`Annotated[redis.Redis, Depends(get_redis)]`) gives a route or dependency the async client for direct commands. The lifespan opens the client with a ping and closes it on shutdown. The cache opens its own connection pool from the same host, port and database, bounded by `CACHE_MAX_CONNECTIONS`, and so does the rate-limit store. Unlike the cache, the rate-limit store raises on a Redis error instead of logging it per command, so `app.core.rate_limiting` can count the outage, let the request through and skip Redis for 5 s ([api.md](api.md#limits)). Eviction is the quiet failure: under `allkeys-lru` a full Redis evicts rate-limit logs and the counting starts again, without any error; the counters therefore belong on their own instance in production (README known issue 31).

## Background jobs

Background work is a taskiq task ([ADR 0009](adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md), [ADR 0010](adr/0010-token-emails-rekeyed-in-the-worker.md)). A task is declared in a module's `tasks.py` and enqueued with `await my_task.kiq(...)` after the commit. [modules.md](modules.md#step-7-the-task) shows one. Do not use FastAPI `BackgroundTasks`.

**Processes.** The API only enqueues. A **worker** runs tasks; the **scheduler** sends cron tasks and delayed retries. Run exactly one scheduler. `run.py dev` starts one worker and one scheduler; the Swarm stack runs them as separate services ([general-structure.md](general-structure.md#runtime-topology)). The retry and Prometheus middlewares belong to the worker only (`app.worker`).

**Delivery is at least once.** The broker is taskiq-redis' `RedisStreamBroker` on db 1, wrapped by `ReconnectingStreamBroker` (`src/app/core/tasks.py`):

- A worker acknowledges a message after the task and its result are stored, and deletes the stream entry together with the acknowledgement, so no task arguments stay in the stream.
- A message that is not acknowledged within 10 minutes (`REDELIVERY_IDLE_MS`) goes to another worker. A task can therefore run twice, and every task is idempotent.
- The stream is trimmed approximately at 100,000 entries.
- While Redis is unreachable, or the consumer group has vanished (`FLUSHALL`), the worker logs `taskiq.broker_unavailable` with the consecutive failures and the outage duration every few seconds, then `taskiq.broker_reconnected`, and resumes on its own. Wrong Redis credentials stop it.

**What a task may receive.** Row ids and primitives, never ORM objects, personal data or secrets: arguments are stored in Redis while queued and logged by taskiq at DEBUG (the app keeps taskiq's loggers at INFO or above). The task reads the rest from the database. A test can prove it: `assert_nothing_personal_in_redis` in `tests/helpers.py`.

**Database session.** A task takes `db: TaskDbSession = TaskiqDepends()`. Each worker process opens its own engine at startup with the API's settings (`DB_POOL_SIZE` per process) and closes it at shutdown. The API and the scheduler open none. The task's service owns the transaction.

**Database errors.** The `task` decorator replaces a database error by `TaskDatabaseError`, whose message holds only the exception types and the SQLSTATE: taskiq logs a failed task's exception and stores it as the result, and PostgreSQL messages can quote row values. Lost connections (SQLSTATE class 08, or none), deadlocks (40P01), serialization failures (40001) and admin shutdowns (57P01) become `TransientTaskDatabaseError`.

**Retries.** A task with `retry_on_error=True` that raises `TransientTaskError` (or a transient database error) runs again after `TASKIQ_RETRY_DELAY_SECONDS`, at most `TASKIQ_RETRY_COUNT` times. Raise `TransientTaskError` only for errors that may pass (network, timeouts, a busy server); everything else fails at once. Its message must hold no personal data. Mail tasks raise `mail_delivery_error(exc) from None`, which picks the transient or the permanent mail error. A delayed retry is a JSON schedule in Redis (`taskiq_retry:*` in db 1) that the scheduler sends, so **a worker without a running scheduler never retries**. Retry schedules never expire (known issue 24).

**Scheduled tasks.** A task with the label `schedule=[{"cron": "0 3 * * *"}]` runs on the scheduler. taskiq 0.13 matches cron fields against UTC, so the time is UTC on every host; do not set `cron_offset`. A periodic task must still be idempotent. Batch large deletes (one transaction per batch, `FOR UPDATE SKIP LOCKED`, no user lock), as `auth.cleanup` does. The built-in scheduled tasks:

| Task name | Schedule (UTC) | Purpose |
|---|---|---|
| `maintenance.taskiq_heartbeat` | every 5 minutes | logs `taskiq.heartbeat`, so worker and scheduler liveness is visible |
| `auth.cleanup` | daily 03:00 | deletes ended sessions and closed email tokens after `AUTH_CLEANUP_RETENTION_DAYS` |

**Known limits.** There is no dead-letter queue and no delivery cap (known issue 21). When the scheduler fails to send a due retry, it does not try again until it restarts (known issue 22).

**Worker metrics.** The worker serves its own Prometheus endpoint; see [monitoring.md](monitoring.md#worker-metrics).

## Inspecting Redis in development

With the dev compose project (the container name depends on the project name; `compose exec` finds it):

```bash
docker compose -f compose.dev.yaml exec redis redis-cli -n 1 XLEN taskiq:tasks     # queued messages
docker compose -f compose.dev.yaml exec redis redis-cli -n 1 --scan --pattern 'taskiq_retry:*'
docker compose -f compose.dev.yaml exec redis redis-cli -n 0 --scan                 # cache keys
```

Never run `FLUSHALL` against a shared Redis. The integration tests flush their own Testcontainers Redis before each test.

## Related

- Configuration: [README → Configuration](../README.md#configuration).
- Why a stream and not a list: [ADR 0009](adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md).
