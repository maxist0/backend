"""Taskiq wiring: shared tasks, broker builders, worker root; logging once per process."""

import asyncio
import os
import subprocess
import sys

from taskiq import TaskiqEvents
from taskiq.schedule_sources import LabelScheduleSource

import app.core.logging as app_logging
import app.core.tasks as app_tasks
import app.modules  # noqa: F401  (registers the module tasks)
from app.core.config import get_settings
from tests.helpers import BACKEND_DIR

# Queued messages carry these names; renaming one strands messages in Redis.
TASK_NAMES = {
    "auth.cleanup",
    "email.send_account_locked_notice",
    "email.send_email_change_completed_notice",
    "email.send_email_change_requested_notice",
    "email.send_token_email",
    "maintenance.taskiq_heartbeat",
}


def test_importing_the_worker_does_not_configure_logging():
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
    )

    assert proc.returncode == 0, proc.stderr[-1500:]
    assert "CONFIGURED False" in proc.stdout


def test_worker_broker_serves_every_task_under_its_name():
    """`app.worker:broker` finds the shared tasks of the core and the modules."""
    code = (
        "import app.worker\n"
        "from app.modules.auth.tasks import send_token_email_task as t\n"
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
    )

    assert proc.returncode == 0, proc.stderr[-1500:]
    names, default = proc.stdout.strip().splitlines()
    assert names == str(sorted(TASK_NAMES))
    assert default == "DEFAULT True"


def test_built_broker_registers_the_worker_logging_hook():
    broker = app_tasks.build_broker(get_settings())

    handlers = broker.event_handlers[TaskiqEvents.WORKER_STARTUP]

    assert app_tasks.configure_worker_logging in handlers
    assert broker.find_task("maintenance.taskiq_heartbeat") is not None


def test_scheduler_finds_the_shared_heartbeat_schedule():
    """The heartbeat is a shared task; the scheduler must still schedule it."""
    broker = app_tasks.build_broker(get_settings())
    retry_source = app_tasks.build_retry_source(get_settings())
    scheduler = app_tasks.build_scheduler(broker, retry_source)
    source, second = scheduler.sources
    assert isinstance(source, LabelScheduleSource)
    assert second is retry_source

    async def _schedules() -> list:
        await source.startup()
        return await source.get_schedules()

    schedules = asyncio.run(_schedules())

    assert scheduler.broker is broker
    assert sorted((s.task_name, s.cron) for s in schedules) == [
        ("auth.cleanup", "0 3 * * *"),
        ("maintenance.taskiq_heartbeat", "*/5 * * * *"),
    ]


def test_worker_startup_hook_configures_logging(monkeypatch):
    calls = []
    monkeypatch.setattr(app_tasks, "setup_logging", calls.append)

    asyncio.run(app_tasks.configure_worker_logging(None))

    assert calls == [get_settings()]


def test_console_colours_are_off_without_a_tty(monkeypatch):
    """Container logs (no TTY) carry no ANSI codes; Windows needs no colorama."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    assert app_logging._console_colors() is False
