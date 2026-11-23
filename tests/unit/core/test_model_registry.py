"""Alembic sees every table through `MODULES`, without application settings.

The expected tables come from the modules' own `models` files, so a new
module needs no edit here, plus the core's own tables. Each module's tests
name its concrete tables.
"""

import os
import subprocess
import sys

from app.core import Base
from app.modules import MODULES
from tests.helpers import BACKEND_DIR, module_tables

# What alembic/env.py does, in a fresh interpreter without ENVIRONMENT (like
# the deployed migrate job, which gets only DATABASE_URL), so no other import
# of this test session can have registered the models or built the settings.
_ALEMBIC_IMPORTS = (
    "import app.modules\n"
    "from app.core import Base, get_settings\n"
    "print(sorted(Base.metadata.tables))\n"
    "print('SETTINGS_BUILT', get_settings.cache_info().currsize)\n"
)


# Tables the core defines itself; importing `app.core` registers them.
CORE_TABLES = {"idempotency_keys"}


def _tables_of_all_modules() -> set[str]:
    return {table for module in MODULES for table in module_tables(module)}


def _expected_tables() -> set[str]:
    return CORE_TABLES | _tables_of_all_modules()


def test_modules_register_every_table_without_environment() -> None:
    """`import app.modules` needs no `ENVIRONMENT` and reads no settings."""
    env = {k: v for k, v in os.environ.items() if k != "ENVIRONMENT"}
    proc = subprocess.run(
        [sys.executable, "-c", _ALEMBIC_IMPORTS],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr[-1500:]
    tables, settings_built = proc.stdout.strip().splitlines()
    assert tables == str(sorted(_expected_tables()))
    assert settings_built == "SETTINGS_BUILT 0"


def test_every_table_belongs_to_a_registered_module_or_the_core() -> None:
    """No orphan models: a table outside every `Module.models` and the core fails."""
    orphans = set(Base.metadata.tables) - _expected_tables()

    assert orphans == set()


def test_every_declared_models_file_registers_its_tables() -> None:
    """A module that declares `models` maps at least one table, on `Base`."""
    declared = {
        module.name: module_tables(module) for module in MODULES if module.models
    }

    assert all(declared.values()), declared
    assert _tables_of_all_modules() <= set(Base.metadata.tables)
