"""`/health/ready` against the real Postgres and Redis containers."""

import os

import pytest
import redis.asyncio as redis
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.database import build_engine


@pytest.fixture
def short_timeout(client):
    fast = get_settings().model_copy(update={"READINESS_TIMEOUT_SECONDS": 1.0})
    client.app.dependency_overrides[get_settings] = lambda: fast
    yield
    client.app.dependency_overrides.clear()


def test_ready_when_database_and_redis_answer(client):
    r = client.get("/health/ready")

    assert r.status_code == 200
    assert r.json() == {"status": "ok", "checks": {"database": "ok", "redis": "ok"}}


def test_not_ready_when_redis_is_unreachable(client, short_timeout):
    real = client.app.state.redis
    unreachable = redis.Redis(host="127.0.0.1", port=1)
    client.app.state.redis = unreachable
    try:
        r = client.get("/health/ready")
    finally:
        client.app.state.redis = real
        client.portal.call(unreachable.aclose)

    assert r.status_code == 503
    assert r.json() == {
        "status": "error",
        "checks": {"database": "ok", "redis": "error"},
    }


def test_not_ready_when_the_database_refuses(client, short_timeout):
    """A wrong password on the real server: fails fast, like an outage."""
    real = client.app.state.engine
    wrong = make_url(os.environ["DATABASE_URL"]).set(password="wrong")
    settings = get_settings().model_copy(
        update={"DATABASE_URL": wrong.render_as_string(hide_password=False)}
    )
    broken = build_engine(settings)
    client.app.state.engine = broken
    try:
        r = client.get("/health/ready")
    finally:
        client.app.state.engine = real
        client.portal.call(broken.dispose)

    assert r.status_code == 503
    assert r.json() == {
        "status": "error",
        "checks": {"database": "error", "redis": "ok"},
    }
    assert "password" not in r.text
