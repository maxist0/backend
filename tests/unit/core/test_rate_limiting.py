"""The rate-limit helper without Redis: parsing, keys, Retry-After, fail-open log.

`hit` runs against a fake log store here. The Redis behaviour (counting,
windows, 429s, a real outage) is covered by
`tests/integration/core/test_rate_limiting.py`.
"""

import logging
import re
from typing import override

import pytest
from cashews.exceptions import CacheBackendInteractionError
from fastapi import Request
from prometheus_client import REGISTRY
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core import RateLimitedError, Settings
from app.core import rate_limiting as rate_limit_module
from app.core.rate_limiting import (
    Rate,
    StoreOutage,
    by_ip,
    by_ipv6_48,
    client_address,
    hit,
    parse_rate,
    rate_limit,
    warn_if_disabled,
)
from tests.helpers import SettingsFactory, log_events, run_async


class TestParseRate:
    @pytest.mark.parametrize(
        ("text", "rate"),
        [
            ("20/minute", Rate(20, 60)),
            ("5/hour", Rate(5, 3_600)),
            ("3/hours", Rate(3, 3_600)),
            ("10/15 minutes", Rate(10, 900)),
            ("1/second", Rate(1, 1)),
            ("100/2 days", Rate(100, 172_800)),
        ],
    )
    def test_valid_limits(self, text: str, rate: Rate) -> None:
        assert parse_rate(text) == rate

    @pytest.mark.parametrize(
        "text",
        ["0/minute", "20/0 minutes", "20 per minute", "20/15minutes", "20/week", ""],
    )
    def test_malformed_limits_are_refused(self, text: str) -> None:
        with pytest.raises(ValueError, match="is not"):
            parse_rate(text)


class TestDefinition:
    @pytest.mark.parametrize(
        "policy",
        [
            "login",
            "Auth.login",
            "auth..login",
            "auth.login-ip",
            "auth.",
            "a." + "b" * 63,
        ],
    )
    def test_malformed_policy_names_fail_when_the_route_is_defined(
        self, policy: str
    ) -> None:
        with pytest.raises(ValueError, match="policy"):
            rate_limit(policy, "1/minute", key=by_ip)

    def test_a_malformed_limit_fails_when_the_route_is_defined(self) -> None:
        with pytest.raises(ValueError, match="is not"):
            rate_limit("probe.unit", "1 per minute", key=by_ip)

    def test_both_series_exist_once_the_route_is_defined(self) -> None:
        rate_limit("probe.unit_series", "1/minute", key=by_ip)

        labels = {"policy": "probe.unit_series"}
        assert REGISTRY.get_sample_value("rate_limit_rejections_total", labels) == 0
        assert REGISTRY.get_sample_value("rate_limit_fail_open_total", labels) == 0


class TestClientAddress:
    @pytest.mark.parametrize(
        ("host", "key"),
        [
            ("203.0.113.7", "203.0.113.7"),
            ("::ffff:203.0.113.7", "203.0.113.7"),
            ("2001:db8:1:2:aaaa:bbbb:cccc:dddd", "2001:db8:1:2::/64"),
            ("2001:DB8:1:2::1", "2001:db8:1:2::/64"),
            ("fe80::1%eth0", "fe80::/64"),
            ("testclient", "testclient"),
            ("", ""),
        ],
    )
    def test_normalisation(self, host: str, key: str) -> None:
        assert client_address(host) == key

    def test_addresses_in_one_ipv6_64_share_a_key(self) -> None:
        assert client_address("2001:db8::1") == client_address("2001:db8::ffff:1")
        assert client_address("2001:db8::1") != client_address("2001:db8:0:1::1")

    def test_without_a_client_address_requests_share_one_key(self) -> None:
        request = Request({"type": "http", "headers": [], "client": None})

        assert by_ip(request) == ""


class _FakeStore:
    """Stands in for the Redis log: records keys, answers fixed values."""

    def __init__(self, count: int = 1, ttl: int = 0) -> None:
        self.count = count
        self.ttl = ttl
        self.keys: list[str] = []
        self.calls: list[tuple[float, float, int, float]] = []

    async def slice_incr(
        self, key: str, start: float, end: float, maxvalue: int, expire: float
    ) -> int:
        self.keys.append(key)
        self.calls.append((start, end, maxvalue, expire))
        return self.count

    async def get_expire(self, key: str) -> int:
        self.keys.append(key)
        return self.ttl


@pytest.fixture
def settings(
    settings_factory: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> Settings:
    """Limits on, with a known signing key; `hit` reads these settings."""
    settings = settings_factory(JWT_SECRET_KEY="k" * 32)
    monkeypatch.setattr(rate_limit_module, "get_settings", lambda: settings)
    return settings


def _store(monkeypatch: pytest.MonkeyPatch, store: object) -> None:
    monkeypatch.setattr(rate_limit_module, "rate_limit_store", store)


@pytest.mark.usefixtures("settings")
class TestHit:
    def test_the_log_is_one_window_with_room_for_one_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore(count=10)
        _store(monkeypatch, store)

        run_async(hit("probe.unit", "10/15 minutes", key="x"))

        [(start, end, maxvalue, expire)] = store.calls
        assert end - start == 900
        assert maxvalue == 11
        assert expire == 900

    def test_the_redis_key_is_pinned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A changed derivation would silently start every window afresh.

        The digest was computed with the derivation of ADR 0013 (HMAC of the
        label under `JWT_SECRET_KEY`, then HMAC of policy and identifier).
        """
        store = _FakeStore()
        _store(monkeypatch, store)

        run_async(hit("probe.unit", "10/15 minutes", key="203.0.113.10"))

        assert store.keys == ["rate_limit:probe.unit:6acc710c0fbff3b7c6427ff5eedc271b"]

    def test_above_the_limit_retry_after_comes_from_the_ttl(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _store(monkeypatch, _FakeStore(count=11, ttl=42))

        with pytest.raises(RateLimitedError) as refused:
            run_async(hit("probe.unit", "10/15 minutes", key="x"))

        # Redis rounds the TTL to the nearest second: one more is never early.
        assert refused.value.headers == {"Retry-After": "43"}
        assert refused.value.status == 429
        assert refused.value.code == "rate_limited"

    @pytest.mark.parametrize(
        ("ttl", "retry_after"), [(60, "60"), (59, "60"), (0, "1"), (-2, "1")]
    )
    def test_retry_after_is_at_least_one_second_and_at_most_the_window(
        self,
        monkeypatch: pytest.MonkeyPatch,
        ttl: int,
        retry_after: str,
    ) -> None:
        _store(monkeypatch, _FakeStore(count=3, ttl=ttl))

        with pytest.raises(RateLimitedError) as refused:
            run_async(hit("probe.unit", "2/minute", key="x"))

        assert refused.value.headers["Retry-After"] == retry_after

    def test_the_key_holds_the_policy_and_an_hmac_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()
        _store(monkeypatch, store)

        run_async(hit("auth.login.ip_username", "1/minute", key="203.0.113.7\0alice"))

        [key] = store.keys
        assert re.fullmatch(r"rate_limit:auth\.login\.ip_username:[0-9a-f]{32}", key)
        assert "203.0.113.7" not in key
        assert "alice" not in key

    def test_the_digest_depends_on_policy_identifier_and_secret(
        self,
        settings_factory: SettingsFactory,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        store = _FakeStore()
        _store(monkeypatch, store)
        run_async(hit("a.b", "1/minute", key="id"))
        run_async(hit("a.b", "1/minute", key="id"))
        run_async(hit("a.c", "1/minute", key="id"))
        run_async(hit("a.b", "1/minute", key="id2"))
        other = settings_factory(JWT_SECRET_KEY="o" * 32)
        monkeypatch.setattr(rate_limit_module, "get_settings", lambda: other)
        run_async(hit("a.b", "1/minute", key="id"))

        digests = [key.rsplit(":", 1)[1] for key in store.keys]
        assert digests[0] == digests[1]
        assert len(set(digests)) == 4

    def test_a_lone_surrogate_in_the_identifier_does_not_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store = _FakeStore()
        _store(monkeypatch, store)

        run_async(hit("a.b", "1/minute", key="\ud800"))

        assert store.keys[0].startswith("rate_limit:a.b:")

    def test_nothing_is_counted_when_limits_are_off(
        self, settings_factory: SettingsFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off = settings_factory(RATE_LIMIT_ENABLED=False)
        monkeypatch.setattr(rate_limit_module, "get_settings", lambda: off)
        store = _FakeStore(count=99)
        _store(monkeypatch, store)

        run_async(hit("probe.unit", "1/minute", key="x"))

        assert store.calls == []

    def test_arguments_are_checked_even_when_limits_are_off(
        self, settings_factory: SettingsFactory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off = settings_factory(RATE_LIMIT_ENABLED=False)
        monkeypatch.setattr(rate_limit_module, "get_settings", lambda: off)

        with pytest.raises(ValueError, match="policy"):
            run_async(hit("Bad", "1/minute", key="x"))
        with pytest.raises(ValueError, match="is not"):
            run_async(hit("probe.unit", "1 per minute", key="x"))


_STRONG = {"JWT_SECRET_KEY": "k" * 48, "FRONTEND_URL": "https://app.example.com"}


class _CountingUnreachableStore:
    """A store whose every command fails like an unreachable Redis."""

    def __init__(self) -> None:
        self.calls = 0

    async def slice_incr(self, *_args: object, **_kwargs: object) -> int:
        self.calls += 1
        raise CacheBackendInteractionError from RedisConnectionError("refused")


class _TtlFailsStore(_FakeStore):
    """Counts a refusal, then fails while reading the TTL."""

    @override
    async def get_expire(self, key: str) -> int:
        raise CacheBackendInteractionError from RedisConnectionError("refused")


def _outage(monkeypatch: pytest.MonkeyPatch, now: list[float]) -> StoreOutage:
    outage = StoreOutage(skip=5, log_interval=60, clock=lambda: now[0])
    monkeypatch.setattr(rate_limit_module, "store_outage", outage)
    return outage


def _fail_open_total(policy: str) -> float:
    labels = {"policy": policy}
    return REGISTRY.get_sample_value("rate_limit_fail_open_total", labels) or 0.0


@pytest.mark.usefixtures("settings")
class TestFailOpen:
    @pytest.fixture
    def store(self, monkeypatch: pytest.MonkeyPatch) -> _CountingUnreachableStore:
        store = _CountingUnreachableStore()
        _store(monkeypatch, store)
        return store

    def test_the_request_passes_and_is_counted(
        self,
        store: _CountingUnreachableStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _outage(monkeypatch, [1_000.0])
        before = _fail_open_total("probe.unit_outage")

        run_async(hit("probe.unit_outage", "1/minute", key="x"))

        assert _fail_open_total("probe.unit_outage") == before + 1
        assert store.calls == 1
        [event] = log_events(caplog, "rate_limit.fail_open")
        assert event["level"] == "warning"
        assert event["policy"] == "probe.unit_outage"
        assert event["error_type"] == "ConnectionError"
        assert event["suppressed"] == 0

    def test_checks_skip_the_store_for_five_seconds_after_an_error(
        self, store: _CountingUnreachableStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1_000.0]
        _outage(monkeypatch, now)
        before = _fail_open_total("probe.unit_outage")
        for _ in range(4):
            run_async(hit("probe.unit_outage", "1/minute", key="x"))
        now[0] += 4.9
        run_async(hit("probe.unit_outage", "1/minute", key="x"))
        assert store.calls == 1

        now[0] += 0.1
        run_async(hit("probe.unit_outage", "1/minute", key="x"))

        assert store.calls == 2
        # Every request let through is counted, skipped or not.
        assert _fail_open_total("probe.unit_outage") == before + 6

    def test_one_line_per_minute_with_the_count_of_the_rest(
        self,
        store: _CountingUnreachableStore,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.WARNING)
        now = [1_000.0]
        _outage(monkeypatch, now)
        for _ in range(3):
            run_async(hit("probe.unit_outage", "1/minute", key="x"))
        now[0] += 30
        run_async(hit("probe.unit_outage", "1/minute", key="x"))
        now[0] += 30

        run_async(hit("probe.unit_outage", "1/minute", key="x"))

        events = log_events(caplog, "rate_limit.fail_open")
        assert [e["suppressed"] for e in events] == [0, 3]
        assert store.calls == 3

    def test_a_failed_ttl_read_still_refuses_with_the_whole_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        now = [1_000.0]
        _outage(monkeypatch, now)
        store = _TtlFailsStore(count=3)
        _store(monkeypatch, store)

        with pytest.raises(RateLimitedError) as refused:
            run_async(hit("probe.unit_outage", "2/minute", key="x"))

        assert refused.value.headers == {"Retry-After": "60"}
        # The error counts as an outage: the next check skips the store.
        run_async(hit("probe.unit_outage", "2/minute", key="x"))
        assert len(store.calls) == 1


class TestIpv6Site:
    @pytest.mark.parametrize(
        ("host", "key"),
        [
            ("2001:db8:1:2:aaaa::1", "2001:db8:1::/48"),
            ("2001:db8:1:ffff::1", "2001:db8:1::/48"),
            ("2001:db8:2::1", "2001:db8:2::/48"),
            ("203.0.113.7", None),
            ("::ffff:203.0.113.7", None),
            ("testclient", None),
        ],
    )
    def test_the_key_is_the_48_and_none_for_ipv4(
        self, host: str, key: str | None
    ) -> None:
        request = Request({"type": "http", "headers": [], "client": (host, 1)})

        assert by_ipv6_48(request) == key


class TestDisabledWarning:
    def test_off_outside_dev_logs_a_warning(
        self, settings_factory: SettingsFactory, caplog: pytest.LogCaptureFixture
    ) -> None:
        settings = settings_factory(
            ENVIRONMENT="prod", RATE_LIMIT_ENABLED=False, **_STRONG
        )

        warn_if_disabled(settings)

        [event] = log_events(caplog, "rate_limit.disabled")
        assert event["level"] == "warning"
        assert event["environment"] == "prod"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"ENVIRONMENT": "dev", "RATE_LIMIT_ENABLED": False},
            {"ENVIRONMENT": "unittest", "RATE_LIMIT_ENABLED": False},
            {"ENVIRONMENT": "prod", "RATE_LIMIT_ENABLED": True, **_STRONG},
        ],
    )
    def test_nothing_is_logged_otherwise(
        self,
        settings_factory: SettingsFactory,
        caplog: pytest.LogCaptureFixture,
        overrides: dict[str, object],
    ) -> None:
        warn_if_disabled(settings_factory(**overrides))

        assert log_events(caplog, "rate_limit.disabled") == []
