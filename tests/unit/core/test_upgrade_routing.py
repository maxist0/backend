"""Router labels and app construction across the FastAPI 0.136 -> 0.163 upgrade.

* 0.137: included routers stay a tree; the metrics middleware resolves the
  full route template, also for one router mounted under two prefixes.
* 0.145/0.146/0.155: removed `**extra` kwargs, deprecated `root_path`.
"""

import warnings

import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import FastAPIDeprecationWarning

from app.core.metrics import REGISTRY
from app.core.middleware import MetricsMiddleware
from app.main import create_app
from tests.helpers import new_test_client


def _endpoint_labels() -> set[str]:
    return {
        sample.labels["endpoint"]
        for metric in REGISTRY.collect()
        if metric.name == "http_requests"
        for sample in metric.samples
        if "endpoint" in sample.labels
    }


class TestRouterTree:
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
    def test_create_app_emits_no_warning_and_sets_no_root_path(self, settings_factory):
        """0.145/0.146/0.155: no removed ``**extra`` kwargs, no ``root_path``.

        ``FastAPI(root_path=...)`` is deprecated since 0.146 (use the server's
        ``--root-path``); removed kwargs raise ``TypeError`` at construction.
        """
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            app = create_app(settings_factory())

        assert [str(w.message) for w in caught] == []
        assert app.root_path == ""

    def test_root_path_argument_is_deprecated(self):
        """GUARD for the test above: the deprecation is real in this version."""
        with pytest.warns(FastAPIDeprecationWarning, match="root_path"):
            FastAPI(root_path="/api")
