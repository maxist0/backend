# src/app/models.py

"""Model registry: import every ORM model module here.

Alembic (``alembic/env.py``) and the test harness import ``Base`` from this
module, so ``Base.metadata`` always contains every table. A new feature with
models adds one import line below.
"""

from app.api.auth import models as _auth_models  # noqa: F401  (registers tables)
from app.common.models import Base

__all__ = ["Base"]
