"""Taskiq wiring: shared tasks, broker builders, the worker root and its logging.

The expected tasks come from the modules' own `tasks` files, so a new module
needs no edit here. Each module's tests name its concrete tasks.
"""

import asyncio
import os
import subprocess
import sys

import pytest
from taskiq import ScheduledTask, TaskiqEvents, TaskiqState
from taskiq.schedule_sources import LabelScheduleSource

import app.core.idempotency as app_idempotency
import app.core.tasks as app_tasks
from app.core.config import get_settings
from app.modules import MODULES
from tests.helpers import BACKEND_DIR, declared_tasks

# The core's own tasks. Queued messages carry these names; renaming one
# strands messages in Redis.
CORE_TASK_NAMES = ["maintenance.taskiq_heartbeat", "maintenance.idempotency_cleanup"]
HEARTBEAT_CRON = "*/5 * * * *"
IDEMPOTENCY_CLEANUP_CRON = "23 * * * *"


def _module_task_names() -> list[str]:
    return [
        declared.task_name
        for module in MODULES
        for declared in declared_tasks(module.tasks)
    ]


def test_core_declares_only_the_heartbeat_and_the_idempotency_cleanup() -> None:
    declared = declared_tasks(app_tasks) + declared_tasks(app_idempotency)

    assert [t.task_name for t in declared] == CORE_TASK_NAMES


def test_core_and_module_task_names_are_unique() -> None:
    """Two tasks with one name would share one slot in the worker's registry."""
    names = CORE_TASK_NAMES + _module_task_names()

    assert len(names) == len(set(names))


def test_importing_the_worker_does_not_configure_logging() -> None:
    """Importing `app.worker` must not call `setup_logging`."""
    code = (
        "import structlog\n"
        "import app.worker\n"
        "print('CONFIGURED', structlog.is_configured())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "CONFIGURED False" in proc.stdout


def test_worker_broker_serves_exactly_the_declared_tasks() -> None:
    """`app.worker:broker` serves the core's tasks and every `Module.tasks`.

    A task that only an import outside `Module.tasks` registers, or a
    declared `tasks` file the worker does not import, fails here.
    """
    code = (
        "import app.worker\n"
        "from app.core.tasks import taskiq_heartbeat as t\n"
        "print(sorted(app.worker.broker.get_all_tasks()))\n"
        "print('DEFAULT', t.kicker().broker is app.worker.broker)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr[-1500:]
    names, default = proc.stdout.strip().splitlines()
    assert names == str(sorted(CORE_TASK_NAMES + _module_task_names()))
    assert default == "DEFAULT True"


def test_built_broker_registers_the_worker_logging_hook() -> None:
    broker = app_tasks.build_broker(get_settings())

    handlers = broker.event_handlers[TaskiqEvents.WORKER_STARTUP]

    assert app_tasks.configure_worker_logging in handlers
    assert broker.find_task("maintenance.taskiq_heartbeat") is not None


def test_scheduler_finds_every_declared_schedule() -> None:
    """The scheduler schedules the shared tasks' `schedule` labels, only those.

    The tasks are shared, not the broker's own; the scheduler must still
    find the core's schedules and every schedule a module task declares.
    """
    broker = app_tasks.build_broker(get_settings())
    retry_source = app_tasks.build_retry_source(get_settings())
    scheduler = app_tasks.build_scheduler(broker, retry_source)
    source, second = scheduler.sources
    assert isinstance(source, LabelScheduleSource)
    assert second is retry_source

    async def _schedules() -> list[ScheduledTask]:
        await source.startup()
        return await source.get_schedules()

    schedules = asyncio.run(_schedules())

    declared = [
        (declared.task_name, entry.get("cron"))
        for module in MODULES
        for declared in declared_tasks(module.tasks)
        for entry in declared.labels.get("schedule", [])
    ]
    assert scheduler.broker is broker
    assert sorted((s.task_name, s.cron) for s in schedules) == sorted(
        [
            ("maintenance.taskiq_heartbeat", HEARTBEAT_CRON),
            ("maintenance.idempotency_cleanup", IDEMPOTENCY_CLEANUP_CRON),
            *declared,
        ]
    )


def test_worker_startup_hook_configures_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(app_tasks, "setup_logging", calls.append)

    asyncio.run(app_tasks.configure_worker_logging(TaskiqState()))

    assert calls == [get_settings()]
