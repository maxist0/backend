"""Alembic sees every table through `MODULES`, without application settings."""

import os
import subprocess
import sys

from app.core import Base
from app.modules import MODULES
from tests.helpers import BACKEND_DIR

EXPECTED_TABLES = {
    "users",
    "password_credentials",
    "auth_audit_log",
    "email_tokens",
    "sessions",
}

# What alembic/env.py does, in a fresh interpreter without ENVIRONMENT (like
# the deployed migrate job, which gets only DATABASE_URL), so no other import
# of this test session can have registered the models or built the settings.
_ALEMBIC_IMPORTS = (
    "import app.modules\n"
    "from app.core import Base, get_settings\n"
    "print(sorted(Base.metadata.tables))\n"
    "print('SETTINGS_BUILT', get_settings.cache_info().currsize)\n"
)


def test_modules_register_every_table_without_environment():
    """`import app.modules` needs no `ENVIRONMENT` and reads no settings."""
    env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
    proc = subprocess.run(
        [sys.executable, "-c", _ALEMBIC_IMPORTS],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stderr[-1500:]
    tables, settings_built = proc.stdout.strip().splitlines()
    assert tables == str(sorted(EXPECTED_TABLES))
    assert settings_built == "SETTINGS_BUILT 0"


def test_modules_models_cover_every_mapped_table():
    """Every mapped table belongs to the `models` of a registered module."""
    module_tables = {
        mapper.local_table.name
        for mapper in Base.registry.mappers
        if any(
            module.models is not None
            and mapper.class_.__module__ == module.models.__name__
            for module in MODULES
        )
    }

    assert module_tables == set(Base.metadata.tables) == EXPECTED_TABLES
