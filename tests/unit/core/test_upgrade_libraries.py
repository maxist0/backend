"""Library behaviour the app relies on that needs no running service.

Covers the SQLAlchemy 2.1 and taskiq 0.13 upgrades. Each test names the
change it covers.
"""

import importlib
import importlib.metadata
import os
import subprocess
import sys
import tomllib

from tests.helpers import BACKEND_DIR


class TestSqlAlchemy21:
    def test_select_is_typed_per_column(self, tmp_path):
        """2.1: ``select(a, b)`` is ``Select[A, B]`` (2.0: ``Select[tuple[A, B]]``).

        Checked with the project's mypy and configuration on a snippet.
        """
        snippet = tmp_path / "select_typing.py"
        snippet.write_text(
            "import uuid\n"
            "from typing import assert_type\n"
            "from sqlalchemy import Select, select\n"
            "from app.modules.users.models import User\n"
            "assert_type(select(User.id, User.username), Select[uuid.UUID, str])\n",
            encoding="utf-8",
        )

        proc = subprocess.run(
            [sys.executable, "-m", "mypy", "--config-file", "pyproject.toml"]
            + [str(snippet)],
            cwd=BACKEND_DIR,
            # Resolve ``app`` from the source tree (the installed package has
            # no py.typed marker yet).
            env={**os.environ, "MYPYPATH": str(BACKEND_DIR / "src")},
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-1000:]

    def test_greenlet_comes_with_the_asyncio_extra(self):
        """2.1: greenlet is no longer a default dependency, only ``[asyncio]``."""
        pyproject = tomllib.loads(
            (BACKEND_DIR / "pyproject.toml").read_text(encoding="utf-8")
        )
        requires = importlib.metadata.requires("sqlalchemy") or []

        assert any(
            dep.startswith("sqlalchemy[asyncio]")
            for dep in pyproject["project"]["dependencies"]
        )
        assert any(
            req.startswith("greenlet") and 'extra == "asyncio"' in req
            for req in requires
        )
        importlib.import_module("greenlet")


class TestTaskiqBroker:
    def test_tasks_are_registered_on_the_broker(self):
        """0.13: broker API unchanged; both mail tasks are registered."""
        from app.core.config import get_settings
        from app.core.tasks import build_broker
        from app.modules.auth.tasks import (
            send_password_reset_email_task,
            send_verification_email_task,
        )

        names = set(build_broker(get_settings()).get_all_tasks())

        assert {
            send_verification_email_task.task_name,
            send_password_reset_email_task.task_name,
        } <= names
