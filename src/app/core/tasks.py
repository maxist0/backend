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

This module must not import module tasks: the composition roots do.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from pathlib import Path

import redis.exceptions
from redis.asyncio import Redis
from taskiq import (
    AckableMessage,
    AsyncBroker,
    ScheduleSource,
    TaskiqEvents,
    TaskiqScheduler,
    TaskiqState,
    async_shared_broker,
)
from taskiq.middlewares import PrometheusMiddleware, SmartRetryMiddleware
from taskiq.schedule_sources import LabelScheduleSource
from taskiq.serializers import JSONSerializer
from taskiq_redis import (
    ListRedisScheduleSource,
    RedisAsyncResultBackend,
    RedisStreamBroker,
)

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)

task = async_shared_broker.task
"""Declare a background task: `@task(task_name="area.action")`.

Enqueue it with `await my_task.kiq(...)`; that needs a default broker, which
the API lifespan and the worker set.
"""

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


class ReconnectingStreamBroker(RedisStreamBroker):
    """`RedisStreamBroker` that deletes acknowledged entries and survives outages.

    taskiq-redis 1.2.4 acknowledges with `XACK` only, so processed messages
    (task arguments included) stay in the stream until `MAXLEN` trims them;
    this broker deletes an entry together with its acknowledgement. It also
    lets every redis-py error escape `listen`, so the taskiq worker process
    died and restarted whenever Redis was briefly unreachable; this broker
    waits and reads again instead.
    """

    def _ack_generator(self, id: str, queue_name: str) -> Callable[[], Awaitable[None]]:
        # taskiq-redis builds the ack of every message here, for newly read
        # and for reclaimed (XAUTOCLAIM) entries alike. Redis 7 has no
        # XACKDEL, so XACK and XDEL run in one MULTI/EXEC transaction.
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


async def configure_worker_logging(state: TaskiqState) -> None:
    """Set up structured logging once per worker or scheduler process.

    Logging is not configured at import. A worker process configures it in
    the `WORKER_STARTUP` hook, the scheduler in `CLIENT_STARTUP` (it is not a
    worker process), the API in its lifespan. Each process fires only one of
    the two events.
    """
    setup_logging(get_settings())


def build_broker(settings: Settings) -> AsyncBroker:
    """Build the Redis stream broker and result backend; opens no connection.

    This is the broker of the API process: it enqueues and has no
    middleware. The worker adds its middlewares in `build_worker_broker`.

    Args:
        settings: Process settings with the `TASKIQ_*` URLs.

    Returns:
        The broker, with the worker logging hook registered.
    """
    broker = ReconnectingStreamBroker(
        settings.TASKIQ_BROKER_URL,
        queue_name=TASK_STREAM,
        # "0": a new group reads the whole stream. With taskiq-redis' "$",
        # entries added before the group exists (for example after Redis
        # lost its data) would never be delivered.
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
