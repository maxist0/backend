"""Built-in auth module: how a user proves identity.

Credentials, refresh-token sessions, email tokens, the audit log and the
flows that use them (register, login, refresh, logout, password reset and
change, email verification). It depends on the users module.
"""

from app.core import Module
from app.modules.auth import models, tasks
from app.modules.auth.admin import VIEWS
from app.modules.auth.router import router

module = Module(
    name="auth",
    router=router,
    models=models,
    tasks=tasks,
    admin_views=VIEWS,
)

__all__ = ["module"]
