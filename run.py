"""Thin launcher for the API (uvicorn on `app.main:app`).

Usage:
    python run.py           # ENVIRONMENT injected by Docker/Kubernetes
    python run.py dev       # sets ENVIRONMENT=dev (reads .env.local) and
                            # also starts a taskiq worker and scheduler
    python run.py unittest  # unit testing environment (not for serving)

Settings, logging and resources are handled by the app itself
(`get_settings()` and the lifespan); this file only selects the
environment and starts the processes.
"""

import argparse
import os
import subprocess
import sys

# psycopg (async) cannot run on Windows' default ProactorEventLoop, which
# uvicorn picks for a single process there. An import string is used as the
# loop factory directly (uvicorn >= 0.36, "--loop <module>:<attr>"). Other
# platforms keep "auto" (uvloop when installed).
UVICORN_LOOP = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"

# Delayed retries are schedules that the scheduler reads every update
# interval (taskiq's default is 60 s), so this bounds how late a retry runs.
SCHEDULER_UPDATE_INTERVAL_SECONDS = 10

# How long a child process gets to shut down before it is killed.
CHILD_STOP_TIMEOUT_SECONDS = 10


def worker_command(workers: int, log_level: str) -> list[str]:
    """Return the command line of the development taskiq worker.

    The `taskiq worker` CLI runs its processes on `asyncio.new_event_loop()`,
    which is the ProactorEventLoop on Windows; psycopg cannot use it. There,
    `python -m app.worker` runs one receiver on a SelectorEventLoop instead.
    """
    if sys.platform == "win32":
        return [sys.executable, "-m", "app.worker"]
    return [
        sys.executable,
        "-m",
        "taskiq",
        "worker",
        "app.worker:broker",
        f"--workers={workers}",
        f"--log-level={log_level}",
    ]


def scheduler_command(log_level: str) -> list[str]:
    """Return the command line of the development taskiq scheduler."""
    return [
        sys.executable,
        "-m",
        "taskiq",
        "scheduler",
        "app.worker:scheduler",
        f"--update-interval={SCHEDULER_UPDATE_INTERVAL_SECONDS}",
        "--skip-first-run",
        f"--log-level={log_level}",
    ]


def stop_process(process: subprocess.Popen[bytes]) -> None:
    """Stop a child process: terminate, wait, then kill if it hangs.

    Terminating (SIGTERM) lets the taskiq worker stop its own worker
    processes; killing it first would orphan them.
    """
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the API server")
    parser.add_argument(
        "env",
        nargs="?",
        default=None,
        choices=["dev", "unittest"],
        help="Environment mode to run (optional; sets ENVIRONMENT)",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Host to bind to (default: from settings)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port to bind to (default: from settings)",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload (development only)",
    )

    args = parser.parse_args()

    # An explicit mode wins over an inherited ENVIRONMENT. Child processes
    # (uvicorn reloader, taskiq worker) inherit it.
    if args.env is not None:
        os.environ["ENVIRONMENT"] = args.env
    elif not os.environ.get("ENVIRONMENT", "").strip():
        parser.error(
            "No mode provided. Set ENVIRONMENT via Docker/Kubernetes, "
            "or run 'python run.py dev' for local development."
        )

    from app.core.config import ConfigurationError, get_settings

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        parser.error(str(exc))

    import uvicorn

    host = args.host if args.host is not None else settings.HOST
    port = args.port if args.port is not None else settings.PORT

    children: list[subprocess.Popen[bytes]] = []

    # In development, run.py also starts the taskiq worker and the scheduler;
    # retries of failed tasks need the scheduler.
    if settings.is_development:
        log_level = settings.LOG_LEVEL.upper()
        children.append(
            subprocess.Popen(worker_command(settings.TASKIQ_WORKERS, log_level))
        )
        children.append(subprocess.Popen(scheduler_command(log_level)))

    try:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=args.reload,
            loop=UVICORN_LOOP,
            log_config=None,  # logging is configured by the app's lifespan
            # The client address in the ASGI scope (access log, audit log)
            # comes from X-Forwarded-For only when the peer is a trusted proxy.
            proxy_headers=True,
            forwarded_allow_ips=settings.FORWARDED_ALLOW_IPS,
        )
    finally:
        for child in children:
            stop_process(child)


if __name__ == "__main__":
    main()
