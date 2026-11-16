# src/app/common/taskiq.py

"""Taskiq broker, result backend and scheduler.

The broker is built from ``get_settings()`` when this module is imported
(the ``@broker.task`` decorators need it); that creates no connection.
Logging is not configured here: the worker configures it once per worker
process in the ``WORKER_STARTUP`` hook, the API in its lifespan.
"""

from __future__ import annotations

from taskiq import TaskiqEvents, TaskiqScheduler, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from app.common.config import get_settings
from app.common.logging import setup_logging

_settings = get_settings()

broker = ListQueueBroker(_settings.TASKIQ_BROKER_URL).with_result_backend(
    RedisAsyncResultBackend(_settings.TASKIQ_RESULT_BACKEND_URL)
)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)


@broker.on_event(TaskiqEvents.WORKER_STARTUP)
async def configure_worker_logging(state: TaskiqState) -> None:
    """Set up structured logging once per worker process."""
    setup_logging(get_settings())


# Import task modules to register @broker.task decorators.
from app.utils import email_tasks as _email_tasks  # noqa: E402,F401
from app.utils import tasks as _tasks  # noqa: E402,F401
