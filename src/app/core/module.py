"""The module contract: how a feature package plugs into the app.

A module defines `module = Module(...)` in its package `__init__` and is
listed once in `app.modules.MODULES`. The composition roots consume it:
`create_app()` mounts `router` and registers `admin_views`, the worker
imports `tasks` and Alembic sees the tables of `models`.
"""

from dataclasses import dataclass
from types import ModuleType

from fastapi import APIRouter
from sqladmin import ModelView


@dataclass(frozen=True, slots=True)
class Module:
    """A feature module.

    Attributes:
        name: Unique module name, for example `auth`.
        router: The module's router. It carries its own `prefix` and `tags`;
            module resources use `APIRouter(lifespan=...)`.
        models: The module's ORM models module. Importing it registers the
            tables on `Base.metadata`.
        tasks: The module's taskiq tasks module (tasks declared with
            `app.core.task`).
        admin_views: sqladmin `ModelView` classes for the admin panel.
    """

    name: str
    router: APIRouter
    models: ModuleType | None = None
    tasks: ModuleType | None = None
    admin_views: tuple[type[ModelView], ...] = ()
