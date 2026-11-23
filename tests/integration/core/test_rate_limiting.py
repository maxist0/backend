"""Rate limits on the Redis container, through routes a module would write.

The probe router uses only the public interface: `rate_limit`, `by_ip`,
`hit` and `RateLimitedError` from `app.core`, `by_user` from
`app.modules.users`. Client addresses come from `X-Forwarded-For` through
uvicorn's proxy-header middleware, as in a deployment behind a proxy.

Time control: `time_machine` with `tick=True`. The log stores each hit under
its timestamp, so frozen time would merge hits; a ticking clock keeps them
apart, and `shift` moves past a window.
"""

import re
import socket
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import time_machine
from fastapi import APIRouter, FastAPI, Response, status
from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from prometheus_client import REGISTRY
from pydantic import BaseModel, ConfigDict, Field

from app.core import (
    RateLimitedError,
    Settings,
    Unauthorized,
    by_ip,
    hit,
    problem_responses,
    rate_limit,
)
from app.core import rate_limiting as rate_limit_module
from app.core.cache import setup_cache
from app.core.config import get_settings
from app.core.rate_limiting import StoreOutage
from app.modules.users import CurrentUser, by_user
from tests.helpers import (
    PII_EMAIL,
    PII_USERNAME,
    assert_nothing_personal_in_redis,
    assert_rate_limited,
    bearer,
    behind_proxy,
    forwarded_for,
    log_events,
    new_test_client,
    rate_limit_keys,
    register_and_login,
    replace_with_string,
    run_parallel,
    set_cache_redis_config,
)

LIMIT = 2
WINDOW = 60
IP_A = "203.0.113.10"
IP_B = "198.51.100.20"

probe = APIRouter(prefix="/probe-limits", tags=["probe"])


class ProbeRequest(BaseModel):
    """A body whose `name` is the rate-limit key."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(max_length=255)


@probe.post(
    "/by-ip",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Probe limited per client address",
    dependencies=[rate_limit("probe.by_ip", f"{LIMIT}/minute", key=by_ip)],
    responses=problem_responses(RateLimitedError),
)
async def limited_by_ip() -> None:
    """Count per client address."""


@probe.post(
    "/by-user",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Probe limited per user",
    dependencies=[rate_limit("probe.by_user", f"{LIMIT}/minute", key=by_user)],
    responses=problem_responses(Unauthorized, RateLimitedError),
)
async def limited_by_user(current_user: CurrentUser) -> None:
    """Count per signed-in user."""
    assert current_user.id


@probe.post(
    "/by-body",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    summary="Probe limited per body value",
    responses=problem_responses(RateLimitedError, 422),
)
async def limited_by_body(body: ProbeRequest) -> None:
    """Count per normalised `name`, as a service does with an email address."""
    await hit("probe.by_body", f"{LIMIT}/minute", key=body.name.lower())


@pytest.fixture
def probe_client(app: FastAPI) -> Iterator[TestClient]:
    """The test app with the probe router, behind the proxy-header middleware."""
    app.include_router(probe, prefix=get_settings().API_PREFIX)
    with new_test_client(behind_proxy(app)) as client:
        yield client


def _by_ip(client: TestClient, ip: str = IP_A) -> HttpResponse:
    return client.post("/api/v1/probe-limits/by-ip", headers=forwarded_for(ip))


def _by_body(client: TestClient, name: str, ip: str = IP_A) -> HttpResponse:
    return client.post(
        "/api/v1/probe-limits/by-body", json={"name": name}, headers=forwarded_for(ip)
    )


def _rejections(policy: str) -> float:
    labels = {"policy": policy}
    return REGISTRY.get_sample_value("rate_limit_rejections_total", labels) or 0.0


def _fail_open(policy: str) -> float:
    labels = {"policy": policy}
    return REGISTRY.get_sample_value("rate_limit_fail_open_total", labels) or 0.0


class TestRefusal:
    def test_the_request_after_the_limit_gets_a_429_problem(
        self, probe_client: TestClient
    ) -> None:
        before = _rejections("probe.by_ip")
        for _ in range(LIMIT):
            assert _by_ip(probe_client).status_code == 204

        r = probe_client.post(
            "/api/v1/probe-limits/by-ip",
            headers={**forwarded_for(IP_A), "X-Request-ID": "rl-1"},
        )

        assert_rate_limited(r, window=WINDOW)
        assert r.json() == {
            "type": "about:blank",
            "title": "Too Many Requests",
            "status": 429,
            "detail": "Too many requests. Try again later.",
            "code": "rate_limited",
            "request_id": "rl-1",
        }
        assert r.headers["x-request-id"] == "rl-1"
        assert "ratelimit" not in r.headers
        assert "ratelimit-policy" not in r.headers
        assert _rejections("probe.by_ip") == before + 1

    def test_retry_after_is_the_time_until_the_log_is_empty(
        self, probe_client: TestClient
    ) -> None:
        for _ in range(LIMIT):
            _by_ip(probe_client)

        r = _by_ip(probe_client)

        # The newest entry is the refused request itself, a moment old.
        assert int(r.headers["retry-after"]) in (WINDOW - 1, WINDOW)

    def test_a_refused_request_counts_too(self, probe_client: TestClient) -> None:
        """A client that keeps sending stays refused until it pauses."""
        start = datetime.now(UTC)
        with time_machine.travel(start, tick=True) as clock:
            assert _by_ip(probe_client).status_code == 204
            clock.shift(30)
            assert _by_ip(probe_client).status_code == 204
            assert _by_ip(probe_client).status_code == 429
            clock.shift(31)

            # The first hit has left the window, the refused one has not.
            assert _by_ip(probe_client).status_code == 429


class TestWindow:
    def test_the_window_slides(self, probe_client: TestClient) -> None:
        """Not a fixed window: a hit counts for exactly one window from its time."""
        start = datetime.now(UTC)
        with time_machine.travel(start, tick=True) as clock:
            assert _by_ip(probe_client).status_code == 204
            clock.shift(50)
            assert _by_ip(probe_client).status_code == 204
            clock.shift(11)

            # 61 s after the first hit: one slot is free again ...
            assert _by_ip(probe_client).status_code == 204
            # ... and only one: the hit at 50 s is still in the window.
            assert _by_ip(probe_client).status_code == 429

    def test_after_the_retry_after_wait_the_route_answers_again(
        self, probe_client: TestClient
    ) -> None:
        start = datetime.now(UTC)
        with time_machine.travel(start, tick=True) as clock:
            for _ in range(LIMIT):
                _by_ip(probe_client)
            r = _by_ip(probe_client)
            assert r.status_code == 429
            clock.shift(int(r.headers["retry-after"]))

            assert _by_ip(probe_client).status_code == 204


class TestKeys:
    def test_each_client_address_has_its_own_count(
        self, probe_client: TestClient
    ) -> None:
        for _ in range(LIMIT):
            _by_ip(probe_client, IP_A)
        assert _by_ip(probe_client, IP_A).status_code == 429

        assert _by_ip(probe_client, IP_B).status_code == 204

    def test_an_ipv6_client_is_counted_per_64_network(
        self, probe_client: TestClient
    ) -> None:
        assert _by_ip(probe_client, "2001:db8:1:2::1").status_code == 204
        assert _by_ip(probe_client, "2001:db8:1:2::ffff").status_code == 204

        assert _by_ip(probe_client, "2001:db8:1:2:abcd::9").status_code == 429
        assert _by_ip(probe_client, "2001:db8:1:3::1").status_code == 204

    def test_each_user_has_its_own_count(self, probe_client: TestClient) -> None:
        alice = bearer(register_and_login(probe_client, username="alice"))
        bob = bearer(register_and_login(probe_client, username="bob"))
        for _ in range(LIMIT):
            assert (
                probe_client.post("/api/v1/probe-limits/by-user", headers=alice)
            ).status_code == 204

        r = probe_client.post("/api/v1/probe-limits/by-user", headers=alice)
        assert_rate_limited(r, window=WINDOW)
        assert (
            probe_client.post("/api/v1/probe-limits/by-user", headers=bob)
        ).status_code == 204

    def test_a_user_limit_does_not_follow_the_address(
        self, probe_client: TestClient
    ) -> None:
        alice = bearer(register_and_login(probe_client, username="alice"))
        for ip in (IP_A, IP_B):
            probe_client.post(
                "/api/v1/probe-limits/by-user", headers={**alice, **forwarded_for(ip)}
            )

        r = probe_client.post(
            "/api/v1/probe-limits/by-user",
            headers={**alice, **forwarded_for("192.0.2.99")},
        )

        assert r.status_code == 429

    def test_a_request_without_a_valid_token_is_401_and_not_counted(
        self, probe_client: TestClient
    ) -> None:
        before = _rejections("probe.by_user")
        for _ in range(LIMIT + 2):
            r = probe_client.post(
                "/api/v1/probe-limits/by-user", headers=bearer("not-a-token")
            )
            assert r.status_code == 401

        assert _rejections("probe.by_user") == before

    def test_a_body_key_is_counted_after_normalisation(
        self, probe_client: TestClient
    ) -> None:
        assert _by_body(probe_client, "Carol", IP_A).status_code == 204
        assert _by_body(probe_client, "carol", IP_B).status_code == 204

        assert _by_body(probe_client, "CAROL", "192.0.2.1").status_code == 429
        assert _by_body(probe_client, "dave", IP_A).status_code == 204


class TestPrivacy:
    def test_redis_keys_hold_no_identifier_in_clear_text(
        self, probe_client: TestClient
    ) -> None:
        _by_ip(probe_client, IP_A)
        _by_body(probe_client, PII_EMAIL)
        _by_body(probe_client, PII_USERNAME)
        token = register_and_login(probe_client, username="alice")
        probe_client.post("/api/v1/probe-limits/by-user", headers=bearer(token))

        keys = rate_limit_keys()

        assert {key.decode().split(":")[1] for key in keys} >= {
            "probe.by_ip",
            "probe.by_body",
            "probe.by_user",
        }
        for key in keys:
            assert re.fullmatch(rb"rate_limit:[a-z0-9_.]+:[0-9a-f]{32}", key), key
        assert_nothing_personal_in_redis(IP_A, "alice")


class TestSwitchedOff:
    @pytest.mark.usefixtures("without_rate_limits")
    def test_rate_limit_enabled_false_lets_every_request_through(
        self, probe_client: TestClient
    ) -> None:
        assert get_settings().RATE_LIMIT_ENABLED is False

        for _ in range(LIMIT + 3):
            assert _by_ip(probe_client).status_code == 204
            assert _by_body(probe_client, "erin").status_code == 204

        assert rate_limit_keys() == []


class TestParallel:
    def test_parallel_requests_get_exactly_the_limit(
        self, probe_client: TestClient
    ) -> None:
        """One atomic script per hit: no two requests take the same last slot."""
        statuses = run_parallel(10, lambda _i: _by_ip(probe_client).status_code)

        assert sorted(statuses) == [204] * LIMIT + [429] * (10 - LIMIT)


def _probe_client_with_store_at(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, port: int
) -> Iterator[TestClient]:
    """The probe app whose cache and rate-limit store use `127.0.0.1:port`.

    `RedisDep` and the broker keep the real Redis, so the app starts; only
    the stores that `setup_cache` configures go elsewhere.
    """

    def setup_elsewhere(settings: Settings) -> None:
        setup_cache(
            settings.model_copy(update={"CACHE_HOST": "127.0.0.1", "CACHE_PORT": port})
        )

    monkeypatch.setattr("app.main.setup_cache", setup_elsewhere)
    app.include_router(probe, prefix=get_settings().API_PREFIX)
    with new_test_client(behind_proxy(app)) as client:
        yield client


@pytest.fixture
def fresh_outage(monkeypatch: pytest.MonkeyPatch) -> StoreOutage:
    """A fresh outage state: an earlier test may have tripped or logged."""
    outage = StoreOutage(skip=5, log_interval=60)
    monkeypatch.setattr(rate_limit_module, "store_outage", outage)
    return outage


@pytest.mark.usefixtures("fresh_outage")
class TestRedisUnreachable:
    @pytest.fixture
    def unreachable_client(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[TestClient]:
        """The stores point at a closed port."""
        yield from _probe_client_with_store_at(app, monkeypatch, 1)

    def test_requests_pass_counted_and_logged_once(
        self, unreachable_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        before = _fail_open("probe.by_ip")

        responses = [_by_ip(unreachable_client) for _ in range(LIMIT + 1)]

        assert [r.status_code for r in responses] == [204] * (LIMIT + 1)
        assert _fail_open("probe.by_ip") == before + LIMIT + 1
        [event] = log_events(caplog, "rate_limit.fail_open")
        assert event["policy"] == "probe.by_ip"
        assert event["level"] == "warning"
        assert event["error_type"]
        assert "cashews" not in {record.name.split(".")[0] for record in caplog.records}


class _SilentServer:
    """A TCP listener that accepts connections and never answers.

    Like a Redis host that hangs: every command waits for the socket timeout.
    """

    def __init__(self) -> None:
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port: int = self._listener.getsockname()[1]
        self._held: list[socket.socket] = []
        self._thread = threading.Thread(target=self._accept, daemon=True)
        self._thread.start()

    def _accept(self) -> None:
        while True:
            try:
                connection, _address = self._listener.accept()
            except OSError:
                return
            self._held.append(connection)

    def close(self) -> None:
        self._listener.close()
        for connection in self._held:
            connection.close()
        self._thread.join(timeout=5)


@pytest.mark.usefixtures("fresh_outage")
class TestRedisHangs:
    @pytest.fixture
    def hanging_client(
        self, app: FastAPI, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[TestClient]:
        server = _SilentServer()
        try:
            yield from _probe_client_with_store_at(app, monkeypatch, server.port)
        finally:
            server.close()

    def test_after_the_first_timeout_the_store_is_skipped(
        self, hanging_client: TestClient
    ) -> None:
        before = _fail_open("probe.by_ip")
        durations: list[float] = []
        for _ in range(5):
            start = time.perf_counter()
            assert _by_ip(hanging_client).status_code == 204
            durations.append(time.perf_counter() - start)

        # The first request waits for the 1 s socket timeout, the rest skip.
        assert durations[0] >= 0.5
        assert max(durations[1:]) < 0.05, durations
        assert _fail_open("probe.by_ip") == before + 5


@pytest.mark.usefixtures("fresh_outage")
class TestRedisErrorReplies:
    def test_a_full_redis_without_eviction_still_enforces_the_limit(
        self, probe_client: TestClient
    ) -> None:
        """Redis lets a script write past `maxmemory` once it has written.

        The log script starts with `ZREMRANGEBYSCORE`, so its `ZADD` is not
        refused with OOM (checked on Redis 7.4.12): a full `noeviction`
        Redis keeps counting. Eviction (`allkeys-lru`) would drop the logs
        instead, which is why the counters need their own instance.
        """
        set_cache_redis_config("maxmemory", 1)
        try:
            statuses = [_by_ip(probe_client).status_code for _ in range(LIMIT + 1)]
        finally:
            set_cache_redis_config("maxmemory", 0)

        assert statuses == [204] * LIMIT + [429]

    def test_an_error_reply_fails_open_too(
        self, probe_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Not only connection errors: any Redis error reply lets the request pass."""
        assert _by_ip(probe_client).status_code == 204
        [key] = rate_limit_keys()
        # The log key now holds a string: the script fails with WRONGTYPE.
        replace_with_string(key)
        before = _fail_open("probe.by_ip")

        r = _by_ip(probe_client)

        assert r.status_code == 204
        assert _fail_open("probe.by_ip") == before + 1
        [event] = log_events(caplog, "rate_limit.fail_open")
        assert event["error_type"] == "ResponseError"
