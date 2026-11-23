"""Which built-in routes accept `Idempotency-Key` (owner decision, ADR 0014).

Every route that creates something or has a side effect is marked. Never
marked: routes whose response carries credentials (login, refresh: a token
or `Set-Cookie` must never be stored or replayed), the logouts (idempotent by
nature) and the 2FA stubs.
"""

from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts

from app.core.idempotency import marks_idempotent
from app.main import create_app
from tests.helpers import SettingsFactory

MARKED = {
    ("POST", "/api/v1/auth/register"),
    ("POST", "/api/v1/auth/password/forgot"),
    ("POST", "/api/v1/auth/password/reset"),
    ("POST", "/api/v1/auth/email/verify"),
    ("POST", "/api/v1/auth/email/verify/resend"),
    ("PUT", "/api/v1/users/me/email"),
    ("PUT", "/api/v1/users/me/password"),
    ("PATCH", "/api/v1/users/me"),
}
NEVER_MARKED = {
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
    ("POST", "/api/v1/auth/logout-all"),
    ("POST", "/api/v1/auth/2fa/enable"),
    ("POST", "/api/v1/auth/2fa/disable"),
    ("POST", "/api/v1/auth/2fa/verify"),
}


def _routes(settings_factory: SettingsFactory) -> dict[tuple[str, str], bool]:
    app = create_app(settings_factory())
    routes: dict[tuple[str, str], bool] = {}
    for context in iter_route_contexts(app.routes):
        dependant: Dependant | None = getattr(context, "dependant", None)
        if dependant is None or context.path_format is None:
            continue
        for method in context.methods or ():
            routes[method, context.path_format] = marks_idempotent(dependant)
    return routes


def test_exactly_the_approved_routes_are_marked(
    settings_factory: SettingsFactory,
) -> None:
    routes = _routes(settings_factory)

    assert {route for route, marked in routes.items() if marked} == MARKED


def test_credential_logout_and_2fa_routes_are_never_marked(
    settings_factory: SettingsFactory,
) -> None:
    routes = _routes(settings_factory)

    assert set(routes) >= NEVER_MARKED
    assert not any(routes[route] for route in NEVER_MARKED)
