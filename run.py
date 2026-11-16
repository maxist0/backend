# run.py
"""
Entry point for the API application.

Usage:
    python run.py           # Environment injected by Docker/Kubernetes
    python run.py dev       # Development environment from .env.local
    python run.py unittest  # Unit testing environment (not for running the server)
"""

import argparse
import os
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="Run the API server")
    parser.add_argument(
        "env",
        nargs="?",
        default=None,
        choices=["dev", "unittest"],
        help="Environment mode to run (optional)",
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
        default=None,
        help="Enable auto-reload (default: from settings)",
    )

    args = parser.parse_args()

    if args.env is None and not os.getenv("ENVIRONMENT"):
        parser.error(
            "No mode provided. Set ENVIRONMENT via Docker/Kubernetes, "
            "or run 'python run.py dev' for local development."
        )

    # Initialize settings BEFORE importing the app
    from app.common.config import init_settings
    from app.common.logging import setup_logging

    settings = init_settings(args.env)
    setup_logging(settings)

    # Now import and run the app
    import uvicorn

    # Use command-line args if provided, otherwise use settings from config
    host = args.host if args.host is not None else settings.HOST
    port = args.port if args.port is not None else settings.PORT

    worker_process = None

    # Start Taskiq worker process automatically in development mode.
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
            reload=False,
            log_config=None,  # Disable uvicorn's default logging configuration
        )
    finally:
        if worker_process is not None and worker_process.poll() is None:
            worker_process.kill()


if __name__ == "__main__":
    main()
