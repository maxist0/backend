"""sqladmin panel, mounted by `create_app()` when `admin_enabled`.

The core provides the authentication backend, the action helpers and the
factory; the views come from the modules (`Module.admin_views`). sqladmin
stores the session factory on the ModelView classes (class attributes), so
one admin-enabled app per process is supported.
"""

import hmac
import uuid
from collections.abc import Iterable

from fastapi import FastAPI
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse

from app.core.config import Settings


def _selected_ids(request: Request) -> list[uuid.UUID]:
    """Primary keys selected in the admin UI.

    sqladmin calls custom actions with `GET ...?pks=<id>,<id>`. Values that
    are not UUIDs are skipped, so malformed input cannot cause a server error.
    """
    raw = request.query_params.get("pks", "")
    ids: list[uuid.UUID] = []
    for value in raw.split(","):
        try:
            ids.append(uuid.UUID(value.strip()))
        except ValueError:
            continue
    return ids


def _session_factory(view: ModelView) -> async_sessionmaker[AsyncSession]:
    """The async session factory sqladmin assigned to `view` (see `create_admin`)."""
    factory = view.session_maker
    if not isinstance(factory, async_sessionmaker):
        raise TypeError("the admin panel requires an async session factory")
    return factory


def _back_to_list(request: Request, identity: str) -> RedirectResponse:
    """Redirect to the model's list view: a fixed admin path.

    Never to the client-supplied `Referer` header (open redirect). Only the
    path is used, so a forged `Host` header cannot change the target either.
    """
    path = request.url_for("admin:list", identity=identity).path
    return RedirectResponse(path, status_code=303)


class AdminAuth(AuthenticationBackend):
    """Session login for the admin panel with the `ADMIN_*` credentials."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(secret_key=settings.ADMIN_SECRET_KEY.get_secret_value())
        self._username = settings.ADMIN_USERNAME
        self._password = settings.ADMIN_PASSWORD

    async def login(self, request: Request) -> bool:
        form = await request.form()
        username, password = form.get("username"), form.get("password")
        if not isinstance(username, str) or not isinstance(password, str):
            return False
        # Constant-time comparison of both values (no timing side channel).
        username_ok = hmac.compare_digest(username.encode(), self._username.encode())
        password_ok = hmac.compare_digest(
            password.encode(), self._password.get_secret_value().encode()
        )
        if username_ok and password_ok:
            request.session.update({"admin": True})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return bool(request.session.get("admin"))


def create_admin(
    app: FastAPI,
    settings: Settings,
    session_factory: async_sessionmaker[AsyncSession],
    views: Iterable[type[ModelView]],
) -> Admin:
    """Mount the admin panel on `app`.

    Args:
        app: The application to mount the panel on.
        settings: Process settings (`ADMIN_*`, `APP_NAME`).
        session_factory: May still be unbound: the lifespan binds it to the
            engine before the first request.
        views: The `ModelView` classes of all modules, in display order.

    Returns:
        The mounted admin.
    """
    auth = AdminAuth(settings)
    admin = Admin(
        app,
        session_maker=session_factory,
        authentication_backend=auth,
        title=f"{settings.APP_NAME} Admin",
    )
    for view in views:
        admin.add_view(view)
    return admin
