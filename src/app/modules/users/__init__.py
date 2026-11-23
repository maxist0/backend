"""Built-in users module: the `User` entity, profile and the current user.

Other modules depend on it through `CurrentUser`, `OptionalUser`, `User`
and the rate-limit key `by_user`.
It depends on no other module.
"""

from app.core import Module
from app.modules.users import models
from app.modules.users.dependencies import CurrentUser, OptionalUser, by_user
from app.modules.users.models import User
from app.modules.users.router import router

module = Module(name="users", router=router, models=models)

__all__ = ["CurrentUser", "OptionalUser", "User", "by_user", "module"]
