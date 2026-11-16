from __future__ import annotations

import os

from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource
from taskiq_redis import ListQueueBroker, RedisAsyncResultBackend

from app.common.config import init_settings
from app.common.logging import setup_logging


env = os.getenv("ENVIRONMENT", "dev")
settings = init_settings(env)
setup_logging(settings)

broker = ListQueueBroker(
    os.getenv("TASKIQ_BROKER_URL", settings.TASKIQ_BROKER_URL)
).with_result_backend(
    RedisAsyncResultBackend(
        os.getenv("TASKIQ_RESULT_BACKEND_URL", settings.TASKIQ_RESULT_BACKEND_URL)
    )
)

scheduler = TaskiqScheduler(
    broker=broker,
    sources=[LabelScheduleSource(broker)],
)


# Import task modules to register @broker.task decorators.
from app.utils import email_tasks as _email_tasks  # noqa: F401,E402
from app.utils import tasks as _tasks  # noqa: F401,E402
