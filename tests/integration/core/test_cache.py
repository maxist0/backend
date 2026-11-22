"""`@cached` and `invalidate` on the Redis container, through the app lifespan.

The probe functions run inside requests, so they use the cache pool the
lifespan opened on the TestClient's event loop.
"""

import os
import pickle
import warnings
from collections.abc import Callable, Iterator

import pytest
import redis as redis_sync
from cashews import Cache
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core import cached, invalidate
from app.core.cache import close_redis, open_redis
from app.core.config import get_settings
from app.main import create_app
from tests.helpers import (
    new_test_client,
    run_async,
)

calls: list[str] = []
unpickled: list[str] = []


@cached(ttl=60, key="probe:profile:{user_id}")
async def _profile(user_id: str) -> dict[str, str | int]:
    calls.append(user_id)
    return {"user_id": user_id, "computed": len(calls)}


def _mark_unpickled() -> str:
    unpickled.append("executed")
    return "pwned"


class _Payload:
    """Unpickling this object calls `_mark_unpickled`."""

    def __reduce__(self) -> tuple[Callable[[], str], tuple[()]]:
        return (_mark_unpickled, ())


def _probe_router() -> APIRouter:
    router = APIRouter(prefix="/probe-cache")

    @router.get("/profiles/{user_id}")
    async def read(user_id: str) -> dict[str, str | int]:
        return await _profile(user_id)

    @router.delete("/profiles", status_code=204)
    async def clear() -> None:
        await invalidate("probe:profile:*")

    return router


@pytest.fixture
def cache_client() -> Iterator[TestClient]:
    """A client of an app with the cache probe routes; Redis starts empty."""
    calls.clear()
    unpickled.clear()
    app = create_app(get_settings())
    app.include_router(_probe_router())
    with new_test_client(app) as client:
        yield client


@pytest.fixture
def raw_redis() -> Iterator[redis_sync.Redis]:
    client = redis_sync.Redis(
        host=get_settings().CACHE_HOST, port=get_settings().CACHE_PORT
    )
    yield client
    client.close()


def test_a_return_value_is_cached(cache_client: TestClient) -> None:
    first = cache_client.get("/probe-cache/profiles/alice").json()
    second = cache_client.get("/probe-cache/profiles/alice").json()

    assert first == second == {"user_id": "alice", "computed": 1}
    assert calls == ["alice"]


def test_a_per_user_key_does_not_leak_between_users(cache_client: TestClient) -> None:
    alice = cache_client.get("/probe-cache/profiles/alice").json()
    bob = cache_client.get("/probe-cache/profiles/bob").json()

    assert alice["user_id"] == "alice"
    assert bob["user_id"] == "bob"
    assert calls == ["alice", "bob"]


def test_invalidate_clears_matching_keys(cache_client: TestClient) -> None:
    cache_client.get("/probe-cache/profiles/alice")
    cache_client.get("/probe-cache/profiles/bob")

    assert cache_client.delete("/probe-cache/profiles").status_code == 204
    again = cache_client.get("/probe-cache/profiles/alice").json()

    assert again["computed"] == 3
    assert calls == ["alice", "bob", "alice"]


def test_values_are_stored_as_json(
    cache_client: TestClient, raw_redis: redis_sync.Redis
) -> None:
    cache_client.get("/probe-cache/profiles/alice")

    stored = raw_redis.get("probe:profile:alice")

    assert stored == b'{"user_id": "alice", "computed": 1}'


def test_a_pickle_written_to_redis_is_never_unpickled(
    cache_client: TestClient, raw_redis: redis_sync.Redis
) -> None:
    """Someone who can write to Redis must not run code in the app."""
    raw_redis.set("probe:profile:mallory", pickle.dumps(_Payload()))

    r = cache_client.get("/probe-cache/profiles/mallory")

    assert unpickled == []
    assert "pwned" not in r.text


class TestRedisClient:
    def test_cache_client_roundtrip_and_close(self) -> None:
        """`open_redis` returns bytes, reads time out after 5 s, and it closes cleanly.

        `close_redis` uses `aclose()`, so no deprecation warning is raised.
        """

        async def scenario() -> tuple[bytes | str | None, object]:
            client = await open_redis(get_settings())
            try:
                await client.set("probe:roundtrip", "v", ex=60)
                value = await client.get("probe:roundtrip")
                timeout = client.connection_pool.connection_kwargs["socket_timeout"]
            finally:
                await close_redis(client)
            return value, timeout

        assert run_async(scenario()) == (b"v", 5)

    def test_cashews_still_calls_the_deprecated_redis_close(self) -> None:
        """Exit condition for the cashews ignore in `[tool.pytest.ini_options]`.

        cashews 7.6.0 closes its redis client with `close()` (deprecated in
        redis-py 5.0.1); the lifespan calls it through `close_cache`. When
        this test fails, cashews uses `aclose()`: delete the ignore and this
        test.
        """
        cache = Cache()
        cache.setup(f"redis://{os.environ['CACHE_HOST']}:{os.environ['CACHE_PORT']}/0")

        async def scenario() -> list[str]:
            await cache.set("probe:cashews", "v", expire=60)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                await cache.close()
            return [str(w.message) for w in caught if w.category is DeprecationWarning]

        messages = run_async(scenario())

        assert any("Call to deprecated close" in m for m in messages)
