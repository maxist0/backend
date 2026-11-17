"""Composition root of the taskiq worker and scheduler.

    taskiq worker app.worker:broker
    taskiq scheduler app.worker:scheduler
    python -m app.worker        # one worker process on a SelectorEventLoop

Importing this module builds the broker from `get_settings()` (so
`ENVIRONMENT` must be set) and makes it the default broker of the shared
tasks. Importing `app.modules` registers every module's tasks
(`Module.tasks`); the core's own tasks come with `app.core.tasks`. Logging
is configured per worker process in the `WORKER_STARTUP` hook, not here.

The worker broker carries the retry and Prometheus middlewares; the API
builds its own broker without them. `python -m app.worker` exists for
Windows, where the `taskiq worker` CLI runs on the ProactorEventLoop that
psycopg cannot use.
"""

import os
import tempfile
from pathlib import Path

# prometheus_client decides once, when it is first imported, whether metric
# values live in memory or in files of PROMETHEUS_MULTIPROC_DIR. The taskiq
# metrics server reads only those files (one per worker process), so the
# directory must be set before `app.modules` imports prometheus_client.
METRICS_DIR = Path(
    os.environ.setdefault(
        "PROMETHEUS_MULTIPROC_DIR",
        str(Path(tempfile.gettempdir()) / "taskiq-metrics"),
    )
)
METRICS_DIR.mkdir(parents=True, exist_ok=True)

import asyncio  # noqa: E402
import contextlib  # noqa: E402

from taskiq import AcknowledgeType, async_shared_broker  # noqa: E402
from taskiq.api import run_receiver_task  # noqa: E402

import app.modules  # noqa: E402, F401  (registers every module's tasks)
from app.core import get_settings  # noqa: E402
from app.core.tasks import (  # noqa: E402
    build_retry_source,
    build_scheduler,
    build_worker_broker,
)

_settings = get_settings()
retry_source = build_retry_source(_settings)
broker = build_worker_broker(_settings, retry_source, METRICS_DIR)
async_shared_broker.default_broker(broker)
scheduler = build_scheduler(broker, retry_source)

__all__ = ["broker", "main", "retry_source", "scheduler"]


async def run_worker() -> None:
    """Run one receiver until cancelled, with broker startup and shutdown."""
    # Set before startup: it selects the WORKER_STARTUP hooks (logging) and
    # lets the Prometheus middleware start its metrics server.
    broker.is_worker_process = True
    await broker.startup()
    try:
        # The same acknowledgement as the CLI default: after the result is
        # stored, so a crash before that redelivers the message.
        await run_receiver_task(broker, ack_time=AcknowledgeType.WHEN_SAVED)
    finally:
        await broker.shutdown()


def main() -> None:
    """Run one worker process on a SelectorEventLoop.

    psycopg's async connections refuse Windows' default ProactorEventLoop;
    the selector loop works on every platform.
    """
    asyncio.run(run_worker(), loop_factory=asyncio.SelectorEventLoop)


if __name__ == "__main__":
    # Ctrl+C reaches every process of the console; asyncio.run has already
    # cancelled the receiver and shut the broker down.
    with contextlib.suppress(KeyboardInterrupt):
        main()
