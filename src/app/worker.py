"""Composition root of the taskiq worker and scheduler.

    taskiq worker app.worker:broker
    taskiq scheduler app.worker:scheduler

Importing this module builds the broker from `get_settings()` (so
`ENVIRONMENT` must be set) and makes it the default broker of the shared
tasks. Importing `app.modules` registers every module's tasks
(`Module.tasks`); the core's own tasks come with `app.core.tasks`. Logging
is configured per worker process in the `WORKER_STARTUP` hook, not here.
"""

from taskiq import async_shared_broker

import app.modules  # noqa: F401  (registers every module's tasks)
from app.core import get_settings
from app.core.tasks import build_broker, build_scheduler

broker = build_broker(get_settings())
async_shared_broker.default_broker(broker)
scheduler = build_scheduler(broker)

__all__ = ["broker", "scheduler"]
