"""`/health/ready` against the real Postgres and Redis containers."""

import os
from collections.abc import Iterator

import pytest
import redis.asyncio as redis
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.database import build_engine
from tests.helpers import (
    app_of,
    call_in_app,
)


@pytest.fixture
def short_timeout(app: FastAPI) -> Iterator[None]:
    """Readiness checks time out after one second."""
    fast = get_settings().model_copy(update={"READINESS_TIMEOUT_SECONDS": 1.0})
    app.dependency_overrides[get_settings] = lambda: fast
    yield
    app.dependency_overrides.clear()


def test_ready_when_database_and_redis_answer(client: TestClient) -> None:
    r = client.get("/health/ready")

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


@pytest.mark.usefixtures("short_timeout")
def test_not_ready_when_redis_is_unreachable(client: TestClient) -> None:
    real = app_of(client).state.redis
    unreachable = redis.Redis(host="127.0.0.1", port=1)
    app_of(client).state.redis = unreachable
    try:
        r = client.get("/health/ready")
    finally:
        app_of(client).state.redis = real
        call_in_app(client, unreachable.aclose)

    assert r.status_code == 503
    assert r.json() == {
        "status": "error",
        "checks": {"database": "ok", "redis": "error"},
    }


@pytest.mark.usefixtures("short_timeout")
def test_not_ready_when_the_database_refuses(client: TestClient) -> None:
    """A wrong password on the real server: fails fast, like an outage."""
    real = app_of(client).state.engine
    wrong = make_url(os.environ["DATABASE_URL"]).set(password="wrong")
    settings = get_settings().model_copy(
        update={"DATABASE_URL": wrong.render_as_string(hide_password=False)}
    )
    broken = build_engine(settings)
    app_of(client).state.engine = broken
    try:
        r = client.get("/health/ready")
    finally:
        app_of(client).state.engine = real
        call_in_app(client, broken.dispose)

    assert r.status_code == 503
    assert r.json() == {
        "status": "error",
        "checks": {"database": "error", "redis": "ok"},
    }
    assert "password" not in r.text
