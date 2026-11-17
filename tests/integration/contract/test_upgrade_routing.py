"""Routing and OpenAPI paths across the FastAPI 0.136 -> 0.163 upgrade.

* 0.137: included routers stay a tree (`app.routes` holds `_IncludedRouter`
  nodes; `scope["route"]` is the router's own `APIRoute` without the
  include prefix). The request middleware resolves the full template.
"""

import pytest
from fastapi.routing import iter_route_contexts
from prometheus_client import REGISTRY

EXPECTED_OPENAPI_PATHS = {
    "/",
    "/api/v1/auth/2fa/disable",
    "/api/v1/auth/2fa/enable",
    "/api/v1/auth/2fa/verify",
    "/api/v1/auth/email/verify",
    "/api/v1/auth/email/verify/resend",
    "/api/v1/auth/login",
    "/api/v1/auth/logout",
    "/api/v1/auth/logout-all",
    "/api/v1/auth/password/forgot",
    "/api/v1/auth/password/reset",
    "/api/v1/auth/refresh",
    "/api/v1/auth/register",
    "/api/v1/users/me",
    "/api/v1/users/me/email",
    "/api/v1/users/me/password",
    "/health",
    "/health/ready",
}


def _request_count(route: str) -> float:
    """Sum of `http_requests_total` over all methods and statuses for `route`."""
    return sum(
        sample.value
        for metric in REGISTRY.collect()
        if metric.name == "http_requests"
        for sample in metric.samples
        if sample.name == "http_requests_total" and sample.labels.get("route") == route
    )


class TestRouterTree:
    def test_openapi_lists_every_route_with_its_full_path(self, app):
        """0.137: the OpenAPI document still sees the prefixed paths."""
        assert set(app.openapi()["paths"]) == EXPECTED_OPENAPI_PATHS

    def test_app_routes_is_a_tree_flattened_by_iter_route_contexts(self, app):
        """0.137: ``app.routes`` no longer lists included routes directly.

        Code that needs every route must use ``iter_route_contexts``; a plain
        iteration over ``app.routes`` misses ``/auth/login`` entirely.
        """
        top_level = {getattr(route, "path", None) for route in app.routes}
        flattened = {context.path for context in iter_route_contexts(app.routes)}

        assert "/api/v1/auth/login" not in top_level
        assert EXPECTED_OPENAPI_PATHS | {"/metrics"} <= flattened
        assert "/admin" in flattened  # the sqladmin mount (ADMIN_ENABLED=true)

    @pytest.mark.parametrize(
        ("method", "path", "router_local_path"),
        [
            ("get", "/api/v1/users/me", "/me"),
            ("post", "/api/v1/auth/login", "/login"),
            ("get", "/health/ready", None),
            ("get", "/", None),
        ],
    )
    def test_metrics_label_is_the_full_route_template(
        self, client, method, path, router_local_path
    ):
        """0.137 REGRESSION GUARD: without the fix the label was ``/me``/``/login``.

        ``scope["route"].path`` lost the include prefix, so labels from
        different routers could collide (two routers with ``/me``).
        """
        # Deltas, not label presence: the global registry is shared by all
        # tests, and the mounted admin app's own "/login" route exists too.
        full_before = _request_count(path)
        local_before = _request_count(router_local_path or "")

        getattr(client, method)(path)

        assert _request_count(path) == full_before + 1
        assert _request_count(router_local_path or "") == local_before
