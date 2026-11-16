# tests/common/test_taskiq_and_logging.py
"""No import-time side effects in the taskiq module; logging set up once per process."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import app.common.logging as app_logging
import app.common.taskiq as app_taskiq
from app.common.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[2]


def test_importing_the_broker_does_not_configure_logging():
    """BE-04: app.common.taskiq used to call init_settings + setup_logging on import."""
    code = (
        "import structlog\n"
        "import app.common.taskiq\n"
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


def test_worker_startup_hook_configures_logging(monkeypatch):
    calls = []
    monkeypatch.setattr(app_taskiq, "setup_logging", calls.append)

    asyncio.run(app_taskiq.configure_worker_logging(None))

    assert calls == [get_settings()]


def test_console_colours_are_off_without_a_tty(monkeypatch):
    """Container logs (no TTY) carry no ANSI codes; Windows needs no colorama."""
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

    assert app_logging._console_colors() is False
