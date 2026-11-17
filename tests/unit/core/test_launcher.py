"""`run.py` starts uvicorn with trusted proxy headers from the settings."""

import sys

import pytest
import uvicorn

from app.core.config import get_settings


def test_uvicorn_trusts_only_the_configured_proxies(monkeypatch, fresh_settings):
    import run

    fresh_settings.setenv("FORWARDED_ALLOW_IPS", "10.0.0.2,10.0.0.3")
    calls: list[dict] = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(sys, "argv", ["run.py", "unittest"])

    run.main()

    [kwargs] = calls
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "10.0.0.2,10.0.0.3"
    assert kwargs["log_config"] is None
    assert get_settings().ENVIRONMENT.value == "unittest"


@pytest.fixture(autouse=True)
def _keep_environment(monkeypatch):
    """`run.main()` writes ENVIRONMENT into os.environ; restore it afterwards."""
    monkeypatch.setenv("ENVIRONMENT", "unittest")


class TestDevChildren:
    """`run.py dev` starts the worker and the scheduler and stops them again."""

    def test_windows_worker_runs_the_selector_loop_entry_point(self, monkeypatch):
        import run

        monkeypatch.setattr(sys, "platform", "win32")

        assert run.worker_command(4, "INFO") == [sys.executable, "-m", "app.worker"]

    def test_other_platforms_run_the_taskiq_worker_cli(self, monkeypatch):
        import run

        monkeypatch.setattr(sys, "platform", "linux")

        command = run.worker_command(4, "INFO")

        assert command[1:5] == ["-m", "taskiq", "worker", "app.worker:broker"]
        assert "--workers=4" in command

    def test_scheduler_reads_retries_more_often_than_once_a_minute(self):
        """Retries are timed schedules; taskiq's default update is 60 s."""
        import run

        command = run.scheduler_command("INFO")

        assert command[1:5] == ["-m", "taskiq", "scheduler", "app.worker:scheduler"]
        assert f"--update-interval={run.SCHEDULER_UPDATE_INTERVAL_SECONDS}" in command
        assert run.SCHEDULER_UPDATE_INTERVAL_SECONDS < 60

    def test_stop_process_ends_a_running_child(self):
        import subprocess

        import run

        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])

        run.stop_process(child)

        assert child.poll() is not None
