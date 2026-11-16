"""Model registry for Alembic."""

import os
import subprocess
import sys

from app.models import Base
from tests.helpers import BACKEND_DIR

EXPECTED_TABLES = {
    "users",
    "password_credentials",
    "auth_audit_log",
    "email_tokens",
    "sessions",
}


def test_registry_alone_registers_every_table():
    """alembic/env.py imports only the registry; it must see all tables.

    A fresh interpreter without ENVIRONMENT, like an Alembic run, so no other
    import of this test session can have registered the models.
    """
    env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "from app.models import Base; print(sorted(Base.metadata.tables))",
        ],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr[-1500:]
    assert proc.stdout.strip() == str(sorted(EXPECTED_TABLES))


def test_every_mapped_class_is_in_the_registry_metadata():
    mapped_tables = {mapper.local_table.name for mapper in Base.registry.mappers}

    assert mapped_tables == set(Base.metadata.tables)
    assert set(Base.metadata.tables) == EXPECTED_TABLES
