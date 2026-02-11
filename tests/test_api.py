"""Basic API tests for root and health endpoints."""

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.common.config import settings


client = TestClient(app)


def _assert_success_response(payload: dict) -> None:
    assert payload.get("success") is True
    assert "metadata" in payload


def test_root_endpoint() -> None:
    response = client.get("/")

    assert response.status_code == 200
    payload = response.json()

    _assert_success_response(payload)

    data = payload.get("data") or {}
    assert data.get("message")
    assert data.get("auth_url") == "/auth"

    # Metrics and docs URLs depend on settings
    if settings.METRICS_ENABLED:
        assert data.get("metrics_url") == "/metrics"


def test_metrics_health_endpoint() -> None:
    if not settings.METRICS_ENABLED:
        pytest.skip("Metrics are disabled in settings")

    response = client.get("/metrics/health")

    assert response.status_code == 200
    payload = response.json()

    _assert_success_response(payload)

    data = payload.get("data") or {}
    assert data.get("status") == "healthy"
    assert data.get("service") == settings.APP_NAME

    uptime_seconds = data.get("uptime_seconds")
    assert isinstance(uptime_seconds, (int, float))
    assert uptime_seconds >= 0
