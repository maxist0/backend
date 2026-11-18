"""Taskiq: the shared task decorator, broker and scheduler builders.

Tasks are declared with `task`, taskiq's shared broker decorator
(`async_shared_broker.task`). Declaring a task needs no settings and opens
no connection, so modules (and Alembic, which imports them for their models)
can be imported without `ENVIRONMENT`. The real broker is built from the
settings by a composition root: `app.worker` for the worker and the
scheduler, the API lifespan for enqueueing. Each one registers it with
`async_shared_broker.default_broker()`; real brokers find shared tasks
through taskiq's global task registry.

Delivery is at least once (Redis stream with acknowledgements, ADR 0009):
a task can run twice, so every task must be idempotent. A task that raises
`TransientTaskError` and sets the label `retry_on_error=True` is retried
after a delay; the delayed retry is a schedule in Redis that the scheduler
sends, so retries need a running scheduler.

A task gets a database session with a `TaskDbSession` parameter. A worker
process opens the engine at `WORKER_STARTUP` and disposes it at
`WORKER_SHUTDOWN`; the API and the scheduler fire the client events only,
so they never open a database pool for tasks.

This module must not import module tasks: the composition roots do.
"""

import asyncio
import functools
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Coroutine,
)
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, override

import psycopg
import redis.exceptions
from redis.asyncio import Redis
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import OperationalError as SAOperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import (
    AckableMessage,
    AsyncBroker,
    ScheduleSource,
    TaskiqDepends,
    TaskiqEvents,
    TaskiqScheduler,
    TaskiqState,
    async_shared_broker,
)
from taskiq.decor import AsyncTaskiqDecoratedTask
from taskiq.middlewares import PrometheusMiddleware, SmartRetryMiddleware
from taskiq.schedule_sources import LabelScheduleSource
from taskiq.serializers import JSONSerializer
from taskiq_redis import (
    ListRedisScheduleSource,
    RedisAsyncResultBackend,
    RedisStreamBroker,
)

from app.core.config import Settings, get_settings
from app.core.database import (
    build_engine,
    close_db,
    create_session_factory,
    ensure_supported_event_loop,
    init_db,
)
from app.core.errors import sqlstate
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)

# Not the former list key `taskiq`: XADD on an existing list key fails with
# WRONGTYPE, and a Redis that still holds old list messages would refuse
# every enqueue.
TASK_STREAM = "taskiq:tasks"

# Approximate trimming (`MAXLEN ~`) keeps the stream bounded; acknowledged
# entries stay in a stream until trimmed. Trimming drops the oldest entries
# whether they were processed or not, so the bound is far above any backlog
# this app expects (a task message is well under 1 KiB, about 50 MiB at most).
TASK_STREAM_MAXLEN = 100_000

# A delivered message that is not acknowledged within this time is handed to
# another worker (XAUTOCLAIM). It must exceed the longest task run: a mail
# send is at most about seven SMTP steps, each bounded by fastapi-mail's
# 60 s timeout (connect, EHLO, STARTTLS, AUTH, MAIL, RCPT, DATA).
REDELIVERY_IDLE_MS = 600_000

# XREADGROUP blocks this long when the stream is empty. It must stay below
# the socket read timeout, or an idle worker gets redis TimeoutError.
XREAD_BLOCK_MS = 2_000
REDIS_SOCKET_TIMEOUT_SECONDS = 5.0

# Pause before the worker reads again after Redis was unreachable.
BROKER_RECONNECT_SECONDS = 5.0

# Nothing in the app reads task results; they only help when inspecting a
# task, so they expire after an hour instead of staying in Redis forever.
TASK_RESULT_TTL_SECONDS = 3_600

# A colon in the prefix breaks ListRedisScheduleSource: it parses the time
# out of `<prefix>:time:<time>` with `key.split(":", 2)[2]`, so overdue
# retries would never be found again.
RETRY_SCHEDULE_PREFIX = "taskiq_retry"


class TransientTaskError(Exception):
    """A task failed for a reason that may pass (network, a busy server).

    The worker retries a task that raises it and sets the label
    `retry_on_error=True`, `TASKIQ_RETRY_COUNT` times,
    `TASKIQ_RETRY_DELAY_SECONDS` apart. Any other exception fails the task
    at once. taskiq logs the exception with its message, so the message must
    not contain personal data.
    """


class TaskDatabaseError(RuntimeError):
    """A task failed on a database error; carries only its types and SQLSTATE.

    The `task` decorator raises it instead of the driver's error: PostgreSQL
    messages can quote row values (`DETAIL: Key (email)=(...)`), and taskiq
    logs a failed task's exception and stores it as the task result.
    """


class TransientTaskDatabaseError(TaskDatabaseError, TransientTaskError):
    """A database error that may pass (lost connection, deadlock); retried."""


# PostgreSQL error codes (Appendix A of the PostgreSQL documentation):
# 40001 serialization_failure and 40P01 deadlock_detected ask the client to
# retry the transaction; 57P01 admin_shutdown ends the connection on a
# server restart; class 08 is connection_exception.
_TRANSIENT_SQLSTATES = frozenset({"40001", "40P01", "57P01"})
_TRANSIENT_SQLSTATE_CLASS = "08"


def task_database_error(exc: DBAPIError | psycopg.Error) -> TaskDatabaseError:
    """Replace a database error by one without the server's message.

    Args:
        exc: The SQLAlchemy-wrapped or raw psycopg error.

    Returns:
        `TransientTaskDatabaseError` for a lost or refused connection, a
        deadlock, a serialization failure or an admin shutdown, otherwise
        `TaskDatabaseError`. The message is `<type> (<driver type>,
        SQLSTATE <code>)`.
    """
    if isinstance(exc, DBAPIError):
        code = sqlstate(exc)
        driver = type(exc.orig).__name__
        # An error raised before the server answered (refused connection,
        # timeout) has no SQLSTATE; SQLAlchemy reports it as OperationalError.
        no_answer = code is None and isinstance(exc, SAOperationalError)
        transient = no_answer or exc.connection_invalidated
    else:
        code = exc.sqlstate
        driver = type(exc).__name__
        transient = code is None and isinstance(exc, psycopg.OperationalError)
    if code is not None:
        transient = (
            transient
            or code in _TRANSIENT_SQLSTATES
            or code.startswith(_TRANSIENT_SQLSTATE_CLASS)
        )
    message = f"{type(exc).__name__} ({driver}, SQLSTATE {code or 'none'})"
    if transient:
        return TransientTaskDatabaseError(message)
    return TaskDatabaseError(message)


def task[**P, R](
    task_name: str | None = None,
    **labels: object,
) -> Callable[
    [Callable[P, Coroutine[Any, Any, R]]],
    AsyncTaskiqDecoratedTask[P, Coroutine[Any, Any, R]],
]:
    """Declare a background task: `@task(task_name="area.action")`.

    taskiq's shared task decorator (`async_shared_broker.task`) around a
    wrapper that turns database errors into `TaskDatabaseError`, so neither
    taskiq's log nor the stored result holds the server's message. Lost
    connections, deadlocks and serialization failures become the transient
    subclass and are retried when the task sets `retry_on_error=True`.
    Every other exception passes through unchanged. The wrapper keeps the
    function's signature (`functools.wraps`), so `TaskiqDepends` parameters
    and `kiq()` typing work as without it.

    Enqueue the task with `await my_task.kiq(...)`; that needs a default
    broker, which the API lifespan and the worker set.

    Args:
        task_name: The task's name in messages; renaming it strands queued
            messages.
        **labels: taskiq labels, for example `retry_on_error=True` or
            `schedule=[{"cron": "*/5 * * * *"}]`.

    Returns:
        The decorator.
    """

    def decorate(
        func: Callable[P, Coroutine[Any, Any, R]],
    ) -> AsyncTaskiqDecoratedTask[P, Coroutine[Any, Any, R]]:
        @functools.wraps(func)
        async def run(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return await func(*args, **kwargs)
            except (DBAPIError, psycopg.Error) as exc:
                error = task_database_error(exc)
            # Raised outside the handler, so the replacement has no
            # `__context__` that a traceback could print.
            raise error

        return async_shared_broker.task(task_name, **labels)(run)

    return decorate


class ReconnectingStreamBroker(RedisStreamBroker):
    """`RedisStreamBroker` that deletes acknowledged entries and survives outages.

    taskiq-redis 1.2.4 acknowledges with `XACK` only, so processed messages
    (task arguments included) stay in the stream until `MAXLEN` trims them;
    this broker deletes an entry together with its acknowledgement. It also
    lets every redis-py error escape `listen`, so the taskiq worker process
    died and restarted whenever Redis was briefly unreachable; this broker
    waits and reads again instead.
    """

    @override
    def _ack_generator(self, id: str, queue_name: str) -> Callable[[], Awaitable[None]]:
        # taskiq-redis builds the ack of every message here, for newly read
        # and for reclaimed (XAUTOCLAIM) entries alike, and passes `id=` as a
        # keyword, so the parameter keeps its name. Redis 7 has no XACKDEL,
        # so XACK and XDEL run in one MULTI/EXEC transaction.
        async def _ack() -> None:
            async with (
                Redis(connection_pool=self.connection_pool) as redis_conn,
                redis_conn.pipeline(transaction=True) as pipe,
            ):
                pipe.xack(queue_name, self.consumer_group_name, id)
                pipe.xdel(queue_name, id)
                await pipe.execute()

        return _ack

    async def listen(self) -> AsyncGenerator[AckableMessage]:
        """Yield stream messages; wait and resume while Redis is unreachable.

        Raises:
            redis.exceptions.AuthenticationError: Wrong credentials do not
                pass by waiting.
            redis.exceptions.ResponseError: Any other Redis error reply than
                a missing consumer group.
        """
        failures = 0
        outage_started = 0.0
        while True:
            try:
                # A Redis that lost its data (restart, FLUSHALL) has no
                # consumer group any more; declaring it again is a no-op
                # otherwise.
                await self._declare_consumer_group()
                if failures:
                    logger.info(
                        "taskiq.broker_reconnected",
                        consecutive_failures=failures,
                        outage_seconds=round(time.monotonic() - outage_started, 1),
                    )
                    failures = 0
                async for message in super().listen():
                    yield message
            except redis.exceptions.AuthenticationError:
                raise
            except (
                redis.exceptions.ConnectionError,
                redis.exceptions.TimeoutError,
                redis.exceptions.ResponseError,
            ) as exc:
                # A missing group is reported as "NOGROUP ..." by XREADGROUP
                # and wrapped as "Command # 2 (XAUTOCLAIM ...) of pipeline
                # caused error: ('NOGROUP ...',)" by redis-py's pipeline.
                if isinstance(
                    exc, redis.exceptions.ResponseError
                ) and "NOGROUP " not in str(exc):
                    raise
                if not failures:
                    outage_started = time.monotonic()
                failures += 1
                logger.warning(
                    "taskiq.broker_unavailable",
                    error_type=type(exc).__name__,
                    consecutive_failures=failures,
                    outage_seconds=round(time.monotonic() - outage_started, 1),
                    retry_in_seconds=BROKER_RECONNECT_SECONDS,
                )
                await asyncio.sleep(BROKER_RECONNECT_SECONDS)


async def configure_worker_logging(_state: TaskiqState) -> None:
    """Set up structured logging once per worker or scheduler process.

    Logging is not configured at import. A worker process configures it in
    the `WORKER_STARTUP` hook, the scheduler in `CLIENT_STARTUP` (it is not a
    worker process), the API in its lifespan. Each process fires only one of
    the two events.
    """
    setup_logging(get_settings())


_STATE_ENGINE = "db_engine"
_STATE_SESSION_FACTORY = "db_session_factory"


async def open_task_db(state: TaskiqState) -> None:
    """Open the database for the tasks of a worker process (`WORKER_STARTUP`).

    The engine uses the same settings as the API's (`build_engine`), so each
    worker process has its own pool of `DB_POOL_SIZE` + `DB_MAX_OVERFLOW`
    connections. Fails at startup, like the API, when the database is
    unreachable or the event loop cannot run psycopg.
    """
    settings = get_settings()
    ensure_supported_event_loop()
    engine = build_engine(settings)
    try:
        await init_db(engine, settings)
    except BaseException:
        await engine.dispose()
        raise
    state[_STATE_ENGINE] = engine
    state[_STATE_SESSION_FACTORY] = create_session_factory(engine)


async def close_task_db(state: TaskiqState) -> None:
    """Dispose the worker's engine (`WORKER_SHUTDOWN`)."""
    state.pop(_STATE_SESSION_FACTORY, None)
    engine = state.pop(_STATE_ENGINE, None)
    if engine is not None:
        await close_db(engine)


def add_task_db_hooks(broker: AsyncBroker) -> None:
    """Open the task database at `WORKER_STARTUP`, close it at `WORKER_SHUTDOWN`.

    Args:
        broker: A broker whose tasks use `TaskDbSession`.
    """
    broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, open_task_db)
    broker.add_event_handler(TaskiqEvents.WORKER_SHUTDOWN, close_task_db)


# A context manager, not a bare async generator: taskiq-dependencies 1.5.7
# throws a task's error into generator dependencies with the three-argument
# `athrow()` (deprecated since Python 3.12) and logs whatever comes back out,
# message included (taskiq_dependencies/ctx.py, `close`). It passes the error
# to a context manager's `__aexit__`, and contextlib re-raises nothing.
@asynccontextmanager
async def get_task_db(
    state: Annotated[TaskiqState, TaskiqDepends()],
) -> AsyncGenerator[AsyncSession]:
    """Provide a session from the worker's session factory for one task run.

    Closing the session rolls back a transaction the task left open.

    Raises:
        RuntimeError: The process is not a worker (no `WORKER_STARTUP`).
    """
    session_factory: async_sessionmaker[AsyncSession] | None = state.get(
        _STATE_SESSION_FACTORY
    )
    if session_factory is None:
        raise RuntimeError(
            "TaskDbSession needs a worker process: the database is opened at "
            "WORKER_STARTUP."
        )
    async with session_factory() as session:
        yield session


TaskDbSession = Annotated[AsyncSession, TaskiqDepends(get_task_db)]
"""A database session for a task, injected by taskiq.

    async def my_task(user_id: str, db: TaskDbSession = TaskiqDepends()) -> None

The `TaskiqDepends()` default only lets `my_task.kiq(user_id)` type-check
without the session; the dependency in the annotation wins. One session per
task run, closed when the task ends; the task's service owns the
transaction and commits.
"""


def build_broker(settings: Settings) -> AsyncBroker:
    """Build the Redis stream broker and result backend; opens no connection.

    This is the broker of the API process: it enqueues and has no
    middleware. The worker adds its middlewares in `build_worker_broker`.

    Args:
        settings: Process settings with the `TASKIQ_*` URLs.

    Returns:
        The broker, with the worker hooks for logging and the task database
        registered; only a worker process fires them.
    """
    broker = ReconnectingStreamBroker(
        settings.TASKIQ_BROKER_URL,
        queue_name=TASK_STREAM,
        # A new group starts at the first entry. With taskiq-redis' default
        # `$`, entries added before the group exists (for example after
        # Redis lost its data) would never be delivered.
        consumer_id="0",
        xread_block=XREAD_BLOCK_MS,
        maxlen=TASK_STREAM_MAXLEN,
        approximate=True,
        idle_timeout=REDELIVERY_IDLE_MS,
        socket_timeout=REDIS_SOCKET_TIMEOUT_SECONDS,
    ).with_result_backend(
        RedisAsyncResultBackend(
            settings.TASKIQ_RESULT_BACKEND_URL,
            result_ex_time=TASK_RESULT_TTL_SECONDS,
            # JSON instead of the default pickle: reading a pickled result
            # lets anyone who can write to Redis run code. Errors are stored
            # as type, module and arguments (taskiq `ExceptionRepr`).
            serializer=JSONSerializer(),
        )
    )
    broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, configure_worker_logging)
    add_task_db_hooks(broker)
    return broker


def build_retry_source(settings: Settings) -> ListRedisScheduleSource:
    """Build the Redis schedule source that holds delayed retries.

    The worker's retry middleware writes to it, the scheduler reads it.

    Args:
        settings: Process settings with `TASKIQ_BROKER_URL`.

    Returns:
        The schedule source; opens no connection.
    """
    # JSON instead of the default pickle: the scheduler loads these values,
    # and unpickling lets anyone who can write to Redis run code.
    return ListRedisScheduleSource(
        settings.TASKIQ_BROKER_URL,
        prefix=RETRY_SCHEDULE_PREFIX,
        serializer=JSONSerializer(),
    )


def build_retry_middleware(
    settings: Settings, retry_source: ScheduleSource
) -> SmartRetryMiddleware:
    """Build the middleware that retries tasks failing with `TransientTaskError`.

    A retry is a one-off schedule in `retry_source`; without it taskiq
    re-sends at once with a `delay` label that the Redis brokers ignore.
    Only tasks with the label `retry_on_error=True` are retried.

    Args:
        settings: Process settings with `TASKIQ_RETRY_COUNT` and
            `TASKIQ_RETRY_DELAY_SECONDS`.
        retry_source: Where delayed retries are stored (`build_retry_source`).

    Returns:
        The retry middleware.
    """
    return SmartRetryMiddleware(
        # taskiq counts the first run: `max_retries=3` means two retries.
        default_retry_count=settings.TASKIQ_RETRY_COUNT + 1,
        default_delay=settings.TASKIQ_RETRY_DELAY_SECONDS,
        # Adds up to one second, so retries of tasks that failed together do
        # not reach the recovering server in the same instant.
        use_jitter=True,
        schedule_source=retry_source,
        types_of_exceptions=(TransientTaskError,),
    )


def build_worker_broker(
    settings: Settings, retry_source: ScheduleSource, metrics_dir: Path
) -> AsyncBroker:
    """Build the worker's broker: `build_broker` plus retries and metrics.

    Only the worker may call this. `PrometheusMiddleware` sets
    `PROMETHEUS_MULTIPROC_DIR` when it is built, which would switch an API
    process's own `/metrics` to multiprocess mode. Its metrics land in
    `metrics_dir` only if `PROMETHEUS_MULTIPROC_DIR` pointed there before
    the process first imported prometheus_client; `app.worker` sets it.

    Args:
        settings: Process settings with the `TASKIQ_*` values.
        retry_source: Where delayed retries are stored (`build_retry_source`).
        metrics_dir: prometheus_client's multiprocess directory.

    Returns:
        The broker with the retry and Prometheus middlewares, and the
        logging hook for the scheduler process as well.
    """
    broker = build_broker(settings)
    # The scheduler builds this broker too, but it is not a worker process,
    # so it fires CLIENT_STARTUP. Without logging set up there, taskiq's
    # kicker would log task arguments at DEBUG (taskiq/kicker.py, `kiq`).
    broker.add_event_handler(TaskiqEvents.CLIENT_STARTUP, configure_worker_logging)
    broker.add_middlewares(
        build_retry_middleware(settings, retry_source),
        PrometheusMiddleware(
            metrics_path=metrics_dir,
            server_port=settings.TASKIQ_METRICS_PORT,
            # taskiq's default is 0.0.0.0, every interface.
            server_addr=settings.TASKIQ_METRICS_HOST,
        ),
    )
    return broker


def build_scheduler(
    broker: AsyncBroker, retry_source: ScheduleSource
) -> TaskiqScheduler:
    """Build the scheduler for the `schedule` labels and the delayed retries.

    Args:
        broker: The broker that receives the scheduled tasks.
        retry_source: The schedule source of the worker's retry middleware.

    Returns:
        A scheduler that sends the due cron tasks of the shared tasks and the
        due retries through `broker`.
    """
    # LabelScheduleSource skips every task whose `task.broker` is not the
    # source's broker (taskiq 0.13, schedule_sources/label_based.py), and a
    # shared task's broker is `async_shared_broker`. The scheduler itself
    # kicks through `broker` (scheduler.py, `on_ready`).
    return TaskiqScheduler(
        broker=broker,
        sources=[LabelScheduleSource(async_shared_broker), retry_source],
    )


@task(
    task_name="maintenance.taskiq_heartbeat",
    schedule=[{"cron": "*/5 * * * *"}],
)
async def taskiq_heartbeat() -> None:
    """Emit a periodic heartbeat log so scheduler and worker liveness is visible."""
    logger.info("taskiq.heartbeat")
