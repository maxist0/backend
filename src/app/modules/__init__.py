"""The registered modules. Adding a module is one line in `MODULES`.

The order is the dependency order: a module may import only modules listed
before it (`tests/unit/core/test_import_direction.py` enforces this).
Importing this package reads no settings and opens no connection, so Alembic
can import it to see every module's tables.
"""

from app.core import Module
from app.modules import auth, users

MODULES: tuple[Module, ...] = (users.module, auth.module)
