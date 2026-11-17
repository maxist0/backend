"""`@cached` and `invalidate` on the Redis container, through the app lifespan.

The probe functions run inside requests, so they use the cache pool the
lifespan opened on the TestClient's event loop.
"""

import pickle

import pytest
import redis as redis_sync
from fastapi import APIRouter

from app.core import cached, invalidate
from app.core.config import get_settings
from app.main import create_app
from tests.helpers import new_test_client

calls: list[str] = []
unpickled: list[str] = []


@cached(ttl=60, key="probe:profile:{user_id}")
async def _profile(user_id: str) -> dict:
    calls.append(user_id)
    return {"user_id": user_id, "computed": len(calls)}


def _mark_unpickled() -> str:
    unpickled.append("executed")
    return "pwned"


class _Payload:
    """Unpickling this object calls `_mark_unpickled`."""

    def __reduce__(self):
        return (_mark_unpickled, ())


def _probe_router() -> APIRouter:
    router = APIRouter(prefix="/probe-cache")

    @router.get("/profiles/{user_id}")
    async def read(user_id: str) -> dict:
        return await _profile(user_id)

    @router.delete("/profiles", status_code=204)
    async def clear() -> None:
        await invalidate("probe:profile:*")

    return router


@pytest.fixture
def cache_client(flush_redis, clean_db):
    calls.clear()
    unpickled.clear()
    app = create_app(get_settings())
    app.include_router(_probe_router())
    with new_test_client(app) as client:
        yield client


@pytest.fixture
def raw_redis():
    client = redis_sync.Redis(
        host=get_settings().CACHE_HOST, port=get_settings().CACHE_PORT
    )
    yield client
    client.close()


def test_a_return_value_is_cached(cache_client):
    first = cache_client.get("/probe-cache/profiles/alice").json()
    second = cache_client.get("/probe-cache/profiles/alice").json()

    assert first == second == {"user_id": "alice", "computed": 1}
    assert calls == ["alice"]


def test_a_per_user_key_does_not_leak_between_users(cache_client):
    alice = cache_client.get("/probe-cache/profiles/alice").json()
    bob = cache_client.get("/probe-cache/profiles/bob").json()

    assert alice["user_id"] == "alice"
    assert bob["user_id"] == "bob"
    assert calls == ["alice", "bob"]


def test_invalidate_clears_matching_keys(cache_client):
    cache_client.get("/probe-cache/profiles/alice")
    cache_client.get("/probe-cache/profiles/bob")

    assert cache_client.delete("/probe-cache/profiles").status_code == 204
    again = cache_client.get("/probe-cache/profiles/alice").json()

    assert again["computed"] == 3
    assert calls == ["alice", "bob", "alice"]


def test_values_are_stored_as_json(cache_client, raw_redis):
    cache_client.get("/probe-cache/profiles/alice")

    stored = raw_redis.get("probe:profile:alice")

    assert stored == b'{"user_id": "alice", "computed": 1}'


def test_a_pickle_written_to_redis_is_never_unpickled(cache_client, raw_redis):
    """Someone who can write to Redis must not run code in the app."""
    raw_redis.set("probe:profile:mallory", pickle.dumps(_Payload()))

    r = cache_client.get("/probe-cache/profiles/mallory")

    assert unpickled == []
    assert "pwned" not in r.text
