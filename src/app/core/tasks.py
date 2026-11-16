"""Taskiq: the shared task decorator, broker and scheduler builders.

Tasks are declared with `task`, taskiq's shared broker decorator
(`async_shared_broker.task`). Declaring a task needs no settings and opens
no connection, so modules (and Alembic, which imports them for their models)
can be imported without `ENVIRONMENT`. The real broker is built from the
settings by a composition root: `app.worker` for the worker and the
scheduler, the API lifespan for enqueueing. Each one registers it with
`async_shared_broker.default_broker()`; real brokers find shared tasks
through taskiq's global task registry.

This module must not import module tasks: the composition roots do.
"""

from taskiq import (
    AsyncBroker,
    TaskiqEvents,
    TaskiqScheduler,
    TaskiqState,
    async_shared_broker,
)
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, setup_logging

logger = get_logger(__name__)

task = async_shared_broker.task
"""Declare a background task: `@task(task_name="area.action")`.

Enqueue it with `await my_task.kiq(...)`; that needs a default broker, which
the API lifespan and the worker set.
"""


async def configure_worker_logging(state: TaskiqState) -> None:
    """Set up structured logging once per worker process.

    Logging is not configured at import: the worker configures it in the
    `WORKER_STARTUP` hook, the API in its lifespan.
    """
    setup_logging(get_settings())


def build_broker(settings: Settings) -> AsyncBroker:
    """Build the Redis broker and result backend; opens no connection.

    Args:
        settings: Process settings with the `TASKIQ_*` URLs.

    Returns:
        The broker, with the worker logging hook registered.
    """
    # socket_timeout=None: the worker waits for jobs with BRPOP without a
    # server timeout. redis-py >= 8 defaults to a 5 s socket read timeout,
    # which turns an idle queue into redis.exceptions.TimeoutError after 5 s
    # (taskiq-redis 1.2.4 only retries ConnectionError). None restores the
    # redis-py 7 read behaviour; connecting stays bounded by redis-py's
    # default socket_connect_timeout (5 s).
    broker = ListQueueBroker(
        settings.TASKIQ_BROKER_URL, socket_timeout=None
    ).with_result_backend(RedisAsyncResultBackend(settings.TASKIQ_RESULT_BACKEND_URL))
    broker.add_event_handler(TaskiqEvents.WORKER_STARTUP, configure_worker_logging)
    return broker


def build_scheduler(broker: AsyncBroker) -> TaskiqScheduler:
    """Build the scheduler for the tasks' `schedule` labels.

    Args:
        broker: The broker that receives the scheduled tasks.

    Returns:
        A scheduler that reads the `schedule` labels of the shared tasks and
        sends the due tasks through `broker`.
    """
    # LabelScheduleSource skips every task whose `task.broker` is not the
    # source's broker (taskiq 0.13, schedule_sources/label_based.py), and a
    # shared task's broker is `async_shared_broker`. The scheduler itself
    # kicks through `broker` (scheduler.py, `on_ready`).
    return TaskiqScheduler(
        broker=broker, sources=[LabelScheduleSource(async_shared_broker)]
    )


@task(
    task_name="maintenance.taskiq_heartbeat",
    schedule=[{"cron": "*/5 * * * *"}],
)
async def taskiq_heartbeat() -> None:
    """Emit a periodic heartbeat log so scheduler and worker liveness is visible."""
    logger.info("taskiq.heartbeat")
