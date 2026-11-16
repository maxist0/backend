# run.py
"""
Thin launcher for the API (uvicorn on ``app.main:app``).

Usage:
    python run.py           # ENVIRONMENT injected by Docker/Kubernetes
    python run.py dev       # sets ENVIRONMENT=dev (reads .env.local) and
                            # also starts a taskiq worker
    python run.py unittest  # unit testing environment (not for serving)

Settings, logging and resources are handled by the app itself
(``get_settings()`` and the lifespan); this file only selects the
environment and starts the processes.
"""

import argparse
import os
import subprocess
import sys


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

    from app.common.config import ConfigurationError, get_settings

    try:
        settings = get_settings()
    except ConfigurationError as exc:
        parser.error(str(exc))

    import uvicorn

    host = args.host if args.host is not None else settings.HOST
    port = args.port if args.port is not None else settings.PORT

    worker_process = None

    # Start a taskiq worker process automatically in development mode.
    if settings.is_development:
        worker_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "taskiq",
                "worker",
                "app.common.taskiq:broker",
                f"--workers={settings.TASKIQ_WORKERS}",
                f"--log-level={settings.LOG_LEVEL.upper()}",
            ]
        )

    try:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=args.reload,
            log_config=None,  # logging is configured by the app's lifespan
        )
    finally:
        if worker_process is not None and worker_process.poll() is None:
            worker_process.kill()


if __name__ == "__main__":
    main()
