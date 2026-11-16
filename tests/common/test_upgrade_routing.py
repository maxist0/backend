# tests/common/test_upgrade_routing.py
"""App assembly and routing across the FastAPI 0.136 -> 0.163 upgrade (step 1.4).

* 0.137: included routers stay a tree (``app.routes`` holds ``_IncludedRouter``
  nodes; ``scope["route"]`` is the router's own ``APIRoute`` without the
  include prefix). The metrics middleware resolves the full template.
* 0.142/0.143: native OpenTelemetry, explicitly disabled in ``create_app``.
* 0.145/0.146/0.155: removed ``**extra`` kwargs, deprecated ``root_path``.
"""

import warnings

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import FastAPIDeprecationWarning
from fastapi.routing import iter_route_contexts


from app.common.config import Settings
from app.common.metrics import REGISTRY
from app.common.middleware import MetricsMiddleware
from app.main import create_app
from tests.helpers import new_test_client

EXPECTED_OPENAPI_PATHS = {
    "/",
    "/auth/2fa/disable",
    "/auth/2fa/enable",
    "/auth/2fa/verify",
    "/auth/email/verify",
    "/auth/email/verify/resend",
    "/auth/login",
    "/auth/logout",
    "/auth/logout-all",
    "/auth/password/forgot",
    "/auth/password/reset",
    "/auth/refresh",
    "/auth/register",
    "/metrics/",
    "/metrics/health",
    "/user/me",
    "/user/me/password",
}


def _settings(**overrides) -> Settings:
    values = {"ENVIRONMENT": "unittest", "ADMIN_ENABLED": False, **overrides}
    return Settings(_env_file=None, **values)


def _endpoint_labels() -> set[str]:
    return {
        sample.labels["endpoint"]
        for metric in REGISTRY.collect()
        if metric.name == "http_requests"
        for sample in metric.samples
        if "endpoint" in sample.labels
    }


def _request_count(endpoint: str) -> float:
    """Sum of ``http_requests_total`` over all methods/statuses for *endpoint*."""
    return sum(
        sample.value
        for metric in REGISTRY.collect()
        if metric.name == "http_requests"
        for sample in metric.samples
        if sample.name == "http_requests_total"
        and sample.labels.get("endpoint") == endpoint
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

        assert "/auth/login" not in top_level
        assert EXPECTED_OPENAPI_PATHS <= flattened
        assert "/admin" in flattened  # the sqladmin mount (ADMIN_ENABLED=true)

    @pytest.mark.parametrize(
        ("method", "path", "router_local_path"),
        [
            ("get", "/user/me", "/me"),
            ("post", "/auth/login", "/login"),
            ("get", "/metrics/health", "/health"),
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

    def test_router_included_under_two_prefixes_gets_two_labels(self):
        """0.137: one APIRoute reachable as /v1/... and /v2/... (API versioning)."""
        router = APIRouter()

        @router.get("/upgrade-items/{item_id}")
        async def read_item(item_id: int, request: Request) -> dict:
            return {"item_id": item_id}

        tiny = FastAPI()
        tiny.add_middleware(MetricsMiddleware)
        tiny.include_router(router, prefix="/v1")
        tiny.include_router(router, prefix="/v2")

        with new_test_client(tiny) as c:
            assert c.get("/v1/upgrade-items/1").status_code == 200
            assert c.get("/v2/upgrade-items/2").status_code == 200

        labels = _endpoint_labels()
        assert "/v1/upgrade-items/{item_id}" in labels
        assert "/v2/upgrade-items/{item_id}" in labels
        assert "/upgrade-items/{item_id}" not in labels


class TestAppConstruction:
    def test_create_app_emits_no_warning_and_sets_no_root_path(self):
        """0.145/0.146/0.155: no removed ``**extra`` kwargs, no ``root_path``.

        ``FastAPI(root_path=...)`` is deprecated since 0.146 (use the server's
        ``--root-path``); removed kwargs raise ``TypeError`` at construction.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            app = create_app(_settings())

        assert [str(w.message) for w in caught] == []
        assert app.root_path == ""

    def test_root_path_argument_is_deprecated(self):
        """GUARD for the test above: the deprecation is real in this version."""
        with pytest.warns(FastAPIDeprecationWarning, match="root_path"):
            FastAPI(root_path="/api")


class TestNativeTelemetryIsOff:
    def test_no_fastapi_otel_log_records_even_with_a_configured_provider(
        self, monkeypatch, app
    ):
        """0.142: FastAPI emits its own OTel records once a logger provider is set.

        ``setup_logging`` installs a global provider when OTLP_LOGS_ENABLED;
        FastAPI would then export e.g. ``fastapi.validation.failed`` and
        unhandled exceptions with message and stack trace. ``create_app``
        disables native telemetry, so nothing is emitted.
        """
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter,
            SimpleLogRecordProcessor,
        )

        exporter = InMemoryLogRecordExporter()
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        # What set_logger_provider() does, but undone after the test (the
        # public setter works only once per process).
        monkeypatch.setattr("opentelemetry._logs._internal._LOGGER_PROVIDER", provider)
        try:
            with new_test_client(app) as c:
                assert c.post("/auth/login", data={}).status_code == 422
        finally:
            provider.shutdown()

        assert exporter.get_finished_logs() == ()
