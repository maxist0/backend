"""sqladmin panel, mounted by `create_app()` when `admin_enabled`.

The core provides the authentication backend, the cross-site request guard,
the helpers for custom actions and the factory; the views come from the
modules (`Module.admin_views`). sqladmin stores the session factory on the
ModelView classes (class attributes), so one admin-enabled app per process
is supported.

CSRF has two layers. sqladmin runs custom actions as GET requests, so a
cookie alone cannot tell a click in the panel from a link on another site:
the session cookie is `SameSite=Strict`, and `FetchMetadataGuard` rejects
state-changing requests that a browser does not mark as same-origin.
"""

import hmac
import re
import uuid
from collections.abc import Iterable
from typing import override

from fastapi import FastAPI
from sqladmin import Admin, ModelView
from sqladmin.authentication import AuthenticationBackend
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Environment, Settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# Absolute lifetime of an admin login. Starlette signs the cookie only when
# the session changes (at login) and checks the signature's age server-side.
SESSION_MAX_AGE_SECONDS = 8 * 60 * 60

# Environments served over plain HTTP; everywhere else the cookie is `Secure`.
_PLAIN_HTTP_ENVIRONMENTS = (Environment.DEV, Environment.UNITTEST)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# sqladmin's custom action route: `/<identity>/action/<slug>` (GET).
_ACTION_PATH = re.compile(r"/[^/]+/action/[^/]+/?$")


class FetchMetadataGuard:
    """Reject state-changing admin requests that are not same-origin.

    State-changing means every unsafe method (login, create, edit, delete,
    import) and every custom action, which sqladmin serves as GET. Such a
    request gets 403 unless its `Sec-Fetch-Site` header is `same-origin`; a
    missing header is refused too (fail safe for sensitive endpoints, OWASP
    CSRF Prevention Cheat Sheet, "Fetch Metadata headers"). All major
    browsers send it since March 2023 (MDN Baseline); older ones cannot use
    the actions. Reading pages needs no header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Answer 403 to a cross-site state change, else pass the request on."""
        if scope["type"] == "http" and _changes_state(scope):
            fetch_site = Headers(scope=scope).get("sec-fetch-site")
            if fetch_site != "same-origin":
                logger.warning(
                    "admin.cross_site_request_rejected",
                    method=scope["method"],
                    fetch_site=(fetch_site or "absent")[:32],
                )
                response = PlainTextResponse("Forbidden", status_code=403)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _changes_state(scope: Scope) -> bool:
    return scope["method"] not in _SAFE_METHODS or bool(
        _ACTION_PATH.search(scope["path"])
    )


def selected_ids(request: Request) -> list[uuid.UUID]:
    """Return the primary keys selected in the admin UI.

    sqladmin calls custom actions with `GET ...?pks=<id>,<id>`. Values that
    are not UUIDs are skipped, so malformed input cannot cause a server error.

    Args:
        request: The request of a custom action.

    Returns:
        The selected ids in request order; empty when none is a valid UUID.
    """
    raw = request.query_params.get("pks", "")
    ids: list[uuid.UUID] = []
    for value in raw.split(","):
        try:
            ids.append(uuid.UUID(value.strip()))
        except ValueError:
            continue
    return ids


def session_factory(view: ModelView) -> async_sessionmaker[AsyncSession]:
    """Return the async session factory sqladmin assigned to `view`.

    `create_admin` hands sqladmin the application's factory, and sqladmin
    stores it on every view class.

    Args:
        view: The model view that runs a custom action.

    Returns:
        The view's async session factory.

    Raises:
        TypeError: The view holds a sync factory; the panel is async only.
    """
    factory = view.session_maker
    if not isinstance(factory, async_sessionmaker):
        raise TypeError("the admin panel requires an async session factory")
    return factory


def back_to_list(request: Request, identity: str) -> RedirectResponse:
    """Redirect to the model's list view: a fixed admin path.

    Never to the client-supplied `Referer` header (open redirect). Only the
    path is used, so a forged `Host` header cannot change the target either.

    Args:
        request: The request of a custom action.
        identity: The sqladmin identity of the view, e.g. `user`.

    Returns:
        A `303 See Other` redirect to the view's list page.
    """
    path = request.url_for("admin:list", identity=identity).path
    return RedirectResponse(path, status_code=303)


class AdminAuth(AuthenticationBackend):
    """Session login for the admin panel with the `ADMIN_*` credentials.

    The session cookie is `HttpOnly`, `SameSite=Strict` and expires
    `SESSION_MAX_AGE_SECONDS` after the login. It is `Secure` in
    every environment except `dev` and `unittest`: `stage` and `test` run
    behind TLS like `prod`, so `is_production` would be too narrow.
    """

    def __init__(self, settings: Settings) -> None:
        # sqladmin passes these keywords on to Starlette's SessionMiddleware.
        super().__init__(
            secret_key=settings.ADMIN_SECRET_KEY.get_secret_value(),
            same_site="strict",
            https_only=settings.ENVIRONMENT not in _PLAIN_HTTP_ENVIRONMENTS,
            max_age=SESSION_MAX_AGE_SECONDS,
        )
        self._username = settings.ADMIN_USERNAME
        self._password = settings.ADMIN_PASSWORD

    @override
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
        client_ip = request.client.host if request.client else None
        if username_ok and password_ok:
            request.session.update({"admin": True})
            # The configured admin name is not a user's personal data; a
            # typed name on failure could be (a mistyped password, say).
            logger.info(
                "admin.login_succeeded", admin=self._username, client_ip=client_ip
            )
            return True
        logger.warning("admin.login_failed", client_ip=client_ip)
        return False

    @override
    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    @override
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
        # Listed before the session middleware, so it runs first.
        middlewares=[Middleware(FetchMetadataGuard)],
        authentication_backend=auth,
        title=f"{settings.APP_NAME} Admin",
    )
    for view in views:
        admin.add_view(view)
    return admin
