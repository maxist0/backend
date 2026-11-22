"""`/health`, `/health/ready` and `/metrics` without Docker.

Readiness runs against stand-ins on `app.state`; the real Postgres and Redis
cases are in `tests/integration/core/test_health.py`.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from app import main
from app.core.config import get_settings
from tests.helpers import SettingsFactory, log_events, new_test_client

_SECRET_DETAIL = "FATAL: password authentication failed for user alice@example.com"


class _Connection:
    async def execute(self, _statement: object) -> None:
        return None


class _Engine:
    """Stand-in for `AsyncEngine.connect()`: works, fails or hangs."""

    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator[_Connection]:
        if self.mode == "fail":
            raise ConnectionRefusedError(_SECRET_DETAIL)
        if self.mode == "hang":
            await asyncio.sleep(60)
        yield _Connection()


class _Redis:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode

    async def ping(self) -> bool:
        if self.mode == "fail":
            raise ConnectionError(_SECRET_DETAIL)
        if self.mode == "hang":
            await asyncio.sleep(60)
        return True


@pytest.fixture
def app(settings_factory: SettingsFactory) -> FastAPI:
    return main.create_app(settings_factory())


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return new_test_client(app, follow_redirects=False)


def _ready(
    app: FastAPI, client: TestClient, *, database: str = "ok", redis: str = "ok"
) -> Response:
    app.state.engine = _Engine(database)
    app.state.redis = _Redis(redis)
    return client.get("/health/ready")


class TestLiveness:
    def test_ok_without_any_dependency(self, client: TestClient) -> None:
        """No lifespan ran: no engine, no Redis. Liveness still answers."""
        r = client.get("/health")

        assert r.status_code == 200
        assert r.json() == {"status": "ok"}
        assert "cache-control" not in r.headers
        assert r.headers["x-request-id"]


class TestReadiness:
    def test_ok_when_both_answer(self, app: FastAPI, client: TestClient) -> None:
        r = _ready(app, client)

        assert r.status_code == 200
        assert r.json() == {
            "status": "ok",
            "checks": {"database": "ok", "redis": "ok"},
        }

    @pytest.mark.parametrize(
        ("database", "redis", "failed"),
        [("fail", "ok", "database"), ("ok", "fail", "redis")],
    )
    def test_503_names_the_failed_check_without_error_text(
        self,
        app: FastAPI,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
        database: str,
        redis: str,
        failed: str,
    ) -> None:
        caplog.set_level(logging.WARNING)

        r = _ready(app, client, database=database, redis=redis)

        assert r.status_code == 503
        body = r.json()
        assert body["status"] == "error"
        assert body["checks"][failed] == "error"
        assert _SECRET_DETAIL not in r.text
        [event] = log_events(caplog, "health.check_failed")
        assert event["check"] == failed
        assert _SECRET_DETAIL not in caplog.text

    def test_a_hanging_dependency_fails_after_the_timeout(
        self, app: FastAPI, client: TestClient
    ) -> None:
        fast = get_settings().model_copy(update={"READINESS_TIMEOUT_SECONDS": 0.05})
        app.dependency_overrides[get_settings] = lambda: fast

        r = _ready(app, client, database="hang", redis="hang")

        assert r.status_code == 503
        assert r.json()["checks"] == {"database": "error", "redis": "error"}


class TestMetricsEndpoint:
    def test_prometheus_text_with_default_collectors(self, client: TestClient) -> None:
        client.get("/health")

        r = client.get("/metrics")

        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/plain")
        assert "http_requests_total" in r.text
        assert 'app_info{environment="unittest"' in r.text
        # Default registry: the collectors that come with prometheus_client.
        assert "python_info" in r.text
        assert "python_gc_objects_collected_total" in r.text

    def test_trailing_slash_redirects_to_the_exact_path(
        self, client: TestClient
    ) -> None:
        r = client.get("/metrics/")

        assert r.status_code == 307
        assert r.headers["location"].endswith("/metrics")

    def test_not_in_the_openapi_document(self, app: FastAPI) -> None:
        assert "/metrics" not in app.openapi()["paths"]

    def test_disabled_metrics_are_not_mounted(
        self, settings_factory: SettingsFactory
    ) -> None:
        app = main.create_app(settings_factory(METRICS_ENABLED=False))

        r = new_test_client(app).get("/metrics")

        assert r.status_code == 404
