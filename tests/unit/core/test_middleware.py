"""The request middleware on a tiny Starlette app.

Request id, access log, security headers, Cache-Control, metric labels and
the 500 path.
"""

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from app.core.middleware import RequestMiddleware, metric_method
from tests.helpers import (
    log_events,
    new_test_client,
)

BODY_LIMIT = 64

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "permissions-policy": "geolocation=()",
}


async def _item(_request: Request) -> Response:
    return PlainTextResponse("item")


async def _cacheable(_request: Request) -> Response:
    return PlainTextResponse("public", headers={"Cache-Control": "max-age=60"})


async def _health(_request: Request) -> Response:
    return PlainTextResponse("ok")


async def _metrics(_request: Request) -> Response:
    return PlainTextResponse("# metrics")


async def _crash(_request: Request) -> Response:
    raise RuntimeError("boom")


@pytest.fixture
def tiny_client() -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/v1/mw-items/{item_id:int}", _item),
            Route("/api/v1/mw-cacheable", _cacheable),
            Route("/health", _health),
            Route("/metrics", _metrics),
            Route("/api/v1/mw-crash", _crash),
        ]
    )
    app.add_middleware(
        RequestMiddleware,
        api_prefix="/api/v1",
        access_log=True,
        metrics=True,
        max_body_bytes=BODY_LIMIT,
    )
    return new_test_client(app)


def _requests(route: str, method: str = "GET", status: str = "200") -> float:
    labels = {"method": method, "route": route, "status": status}
    return REGISTRY.get_sample_value("http_requests_total", labels) or 0.0


def _app_log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Our records only; the test client logs the full URL itself."""
    return "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("app.")
    )


class TestRequestId:
    def test_a_valid_client_id_is_echoed(self, tiny_client: TestClient) -> None:
        r = tiny_client.get("/health", headers={"X-Request-ID": "trace.A-1_b"})

        assert r.headers["x-request-id"] == "trace.A-1_b"

    @pytest.mark.parametrize(
        "sent",
        ["has space", "x" * 129, "semi;colon", "", "back\\slash", 'quo"te'],
        ids=["space", "too-long", "semicolon", "empty", "backslash", "quote"],
    )
    def test_an_invalid_client_id_is_replaced_by_a_uuid(
        self, tiny_client: TestClient, sent: str
    ) -> None:
        r = tiny_client.get("/health", headers={"X-Request-ID": sent})

        assert r.headers["x-request-id"] != sent
        uuid.UUID(r.headers["x-request-id"])

    def test_without_a_client_id_each_response_gets_a_new_uuid(
        self, tiny_client: TestClient
    ) -> None:
        first = tiny_client.get("/health").headers["x-request-id"]
        second = tiny_client.get("/health").headers["x-request-id"]

        assert first != second
        uuid.UUID(first)

    def test_the_id_is_bound_to_the_logs_of_the_request(
        self, tiny_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        tiny_client.get("/health", headers={"X-Request-ID": "bound-1"})

        [event] = log_events(caplog, "http.access")
        assert event["request_id"] == "bound-1"


class TestAccessLog:
    def test_one_event_with_the_agreed_fields(
        self, tiny_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        tiny_client.get(
            "/api/v1/mw-items/7?token=SUPERSECRETQUERY",
            headers={
                "Referer": "https://example.org/?reset=SECRETREFERER",
                "User-Agent": "FingerprintAgent/1.0",
                "X-Forwarded-For": "203.0.113.9",
            },
        )

        [event] = log_events(caplog, "http.access")
        assert set(event) >= {
            "request_id",
            "method",
            "path",
            "route",
            "status",
            "duration_ms",
            "client_ip",
        }
        assert event["path"] == "/api/v1/mw-items/7"
        assert event["route"] == "/api/v1/mw-items/{item_id}"
        assert event["status"] == 200
        # The scope's client, never a client-supplied forwarding header.
        assert event["client_ip"] == "testclient"
        text = _app_log_text(caplog)
        assert "SUPERSECRETQUERY" not in text
        assert "SECRETREFERER" not in text
        assert "FingerprintAgent" not in text
        assert "203.0.113.9" not in text

    def test_disabled_access_log_emits_nothing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = Starlette(routes=[Route("/health", _health)])
        app.add_middleware(
            RequestMiddleware,
            api_prefix="/api/v1",
            access_log=False,
            metrics=False,
            max_body_bytes=BODY_LIMIT,
        )
        caplog.set_level(logging.INFO)

        new_test_client(app).get("/health")

        assert log_events(caplog, "http.access") == []


class TestHeaders:
    def test_security_headers_and_no_store_under_the_api_prefix(
        self, tiny_client: TestClient
    ) -> None:
        r = tiny_client.get("/api/v1/mw-items/1")

        for name, value in SECURITY_HEADERS.items():
            assert r.headers[name] == value
        assert r.headers["cache-control"] == "no-store"
        assert "x-xss-protection" not in r.headers
        assert "x-process-time" not in r.headers

    def test_outside_the_api_prefix_no_cache_control_is_added(
        self, tiny_client: TestClient
    ) -> None:
        r = tiny_client.get("/health")

        for name, value in SECURITY_HEADERS.items():
            assert r.headers[name] == value
        assert "cache-control" not in r.headers

    def test_a_route_set_cache_control_is_kept(self, tiny_client: TestClient) -> None:
        r = tiny_client.get("/api/v1/mw-cacheable")

        assert r.headers["cache-control"] == "max-age=60"

    def test_a_404_gets_the_headers_too(self, tiny_client: TestClient) -> None:
        r = tiny_client.get("/api/v1/nothing-here")

        assert r.status_code == 404
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"
        assert r.headers["x-request-id"]


class TestMetricLabels:
    def test_route_label_is_the_template(self, tiny_client: TestClient) -> None:
        before = _requests("/api/v1/mw-items/{item_id}")

        tiny_client.get("/api/v1/mw-items/41")
        tiny_client.get("/api/v1/mw-items/42")

        assert _requests("/api/v1/mw-items/{item_id}") == before + 2

    def test_unmatched_paths_collapse_into_one_label(
        self, tiny_client: TestClient
    ) -> None:
        segments = [uuid.uuid4().hex for _ in range(3)]
        before = _requests("unmatched", status="404")

        for segment in segments:
            assert tiny_client.get(f"/zz-{segment}").status_code == 404

        assert _requests("unmatched", status="404") == before + 3
        routes = {
            sample.labels.get("route")
            for metric in REGISTRY.collect()
            for sample in metric.samples
        }
        assert not any(segment in str(routes) for segment in segments)

    def test_unknown_method_collapses_into_other(self, tiny_client: TestClient) -> None:
        invented = "ZZ" + uuid.uuid4().hex[:12].upper()

        tiny_client.request(invented, "/health")

        labels = {
            sample.labels.get("method")
            for metric in REGISTRY.collect()
            for sample in metric.samples
        }
        assert invented not in labels
        assert _requests("/health", method="other", status="405") >= 1

    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
    def test_standard_methods_keep_their_label(self, method: str) -> None:
        assert metric_method(method) == method

    def test_the_metrics_endpoint_is_not_instrumented(
        self, tiny_client: TestClient
    ) -> None:
        before = _requests("/metrics")

        tiny_client.get("/metrics")

        assert _requests("/metrics") == before
        assert REGISTRY.get_sample_value(
            "http_request_duration_seconds_count",
            {"method": "GET", "route": "/metrics"},
        ) in (None, 0.0)


class TestUnhandledException:
    def test_500_with_headers_metric_and_access_log(
        self, tiny_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        before = _requests("/api/v1/mw-crash", status="500")

        r = tiny_client.get("/api/v1/mw-crash", headers={"X-Request-ID": "c-1"})

        assert r.status_code == 500
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["request_id"] == "c-1"
        assert r.headers["x-request-id"] == "c-1"
        assert r.headers["x-frame-options"] == "DENY"
        assert r.headers["cache-control"] == "no-store"
        assert _requests("/api/v1/mw-crash", status="500") == before + 1
        [event] = log_events(caplog, "http.access")
        assert event["status"] == 500
