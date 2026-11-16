# tests/security/test_startup_and_exposure.py
"""
Startup guard, admin opt-in, metric cardinality and harness wiring.

  TestStartupGuard ........... K2 (app part): SEC-01
  TestAdminOptIn ............. K2 / H5: SEC-01, SEC-09, SEC-10
  TestMetricsCardinality ..... H11: OBS-08, SEC-14
  TestMetricsCardinality ..... also NEW-7 (bounded `method` label)
  TestPasswordHashSettings ... NEW-7 (OWASP lower bounds for Argon2 settings)
  TestHarness ................ H13: TST-04..07, Known issue 1 (Redis wiring)

Target contract assumed by these tests:
  - Settings(...) raises (ValueError / pydantic ValidationError / RuntimeError)
    when ENVIRONMENT is not dev/unittest and JWT_SECRET_KEY, ADMIN_PASSWORD or
    ADMIN_SECRET_KEY is still the code default, or JWT_SECRET_KEY has < 32 chars.
  - JWT_SECRET_KEY, ADMIN_PASSWORD, ADMIN_SECRET_KEY are SecretStr.
  - env var ADMIN_ENABLED=false (or unset outside dev) -> /admin is not mounted (404).
"""

import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app.common.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[2]

STRONG = {
    "JWT_SECRET_KEY": "k" * 48,
    "ADMIN_PASSWORD": "a-long-random-admin-password-1234",
    "ADMIN_SECRET_KEY": "s" * 48,
}
DEFAULTS = {
    "JWT_SECRET_KEY": "your-super-secret-jwt-key-change-in-production",
    "ADMIN_PASSWORD": "admin",
    "ADMIN_SECRET_KEY": "admin-secret-key-change-in-production",
}


def _settings(env: str, **overrides):
    return Settings(_env_file=None, ENVIRONMENT=env, **overrides)


class TestStartupGuard:
    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    @pytest.mark.parametrize(
        "unset", ["JWT_SECRET_KEY", "ADMIN_PASSWORD", "ADMIN_SECRET_KEY"]
    )
    def test_default_secret_outside_dev_refuses_to_start(self, env, unset):
        kwargs = {
            k: v for k, v in STRONG.items() if k != unset
        }  # `unset` keeps its default

        with pytest.raises((ValueError, RuntimeError)):
            _settings(env, ADMIN_ENABLED=True, **kwargs)

    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    @pytest.mark.parametrize("admin_enabled", [False, None], ids=["false", "unset"])
    def test_admin_disabled_with_default_admin_secrets_starts(self, env, admin_enabled):
        """Admin secrets are only checked when the admin is enabled."""
        # An explicit None overrides the process-wide ADMIN_ENABLED=true of conftest.
        s = _settings(
            env, JWT_SECRET_KEY=STRONG["JWT_SECRET_KEY"], ADMIN_ENABLED=admin_enabled
        )

        assert s.admin_enabled is False

    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    def test_admin_disabled_still_requires_strong_jwt_secret(self, env):
        with pytest.raises((ValueError, RuntimeError)):
            _settings(env, ADMIN_ENABLED=False)

    @pytest.mark.parametrize(
        "unset", ["JWT_SECRET_KEY", "ADMIN_PASSWORD", "ADMIN_SECRET_KEY"]
    )
    def test_explicitly_passing_the_default_value_is_also_refused(self, unset):
        kwargs = dict(STRONG, **{unset: DEFAULTS[unset]})

        with pytest.raises((ValueError, RuntimeError)):
            _settings("prod", ADMIN_ENABLED=True, **kwargs)

    def test_short_jwt_secret_outside_dev_refuses_to_start(self):
        with pytest.raises((ValueError, RuntimeError)):
            _settings("prod", **dict(STRONG, JWT_SECRET_KEY="k" * 31))

    def test_prod_with_strong_secrets_starts(self):
        s = _settings("prod", **STRONG)
        assert s.is_production

    @pytest.mark.parametrize("env", ["dev", "unittest"])
    def test_defaults_are_still_allowed_in_dev_and_unittest(self, env):
        """GUARD: local development and the test suite keep working."""
        assert _settings(env).ENVIRONMENT.value == env

    def test_secrets_are_not_exposed_by_repr_or_str(self):
        s = _settings("unittest", **STRONG)
        text = repr(s) + str(s)
        for value in STRONG.values():
            assert value not in text

    def test_default_secrets_are_not_exposed_by_repr(self):
        text = repr(_settings("unittest"))
        for value in (DEFAULTS["JWT_SECRET_KEY"], DEFAULTS["ADMIN_SECRET_KEY"]):
            assert value not in text


def _admin_status_in_subprocess(env_overrides: dict[str, str | None]) -> list[int]:
    """Import the real app in a fresh interpreter and request /admin routes.

    ENVIRONMENT=unittest is inherited from the test process.

    The TestClient is NOT entered as a context manager, so no lifespan (and no
    database) is needed: an unmounted /admin yields a plain 404.
    """
    code = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app, follow_redirects=False)\n"
        "print('STATUS', c.get('/admin/login').status_code, c.get('/admin/').status_code)\n"
    )
    env = dict(os.environ)
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    match = re.search(r"STATUS (\d+) (\d+)", proc.stdout)
    assert match, f"subprocess failed:\n{proc.stdout[-500:]}\n{proc.stderr[-1500:]}"
    return [int(match.group(1)), int(match.group(2))]


class TestAdminOptIn:
    def test_admin_is_not_mounted_when_admin_enabled_is_false(self):
        statuses = _admin_status_in_subprocess({"ADMIN_ENABLED": "false"})
        assert statuses == [404, 404]

    def test_admin_is_not_mounted_by_default_outside_dev(self):
        """ADMIN_ENABLED unset: true only in dev. ENVIRONMENT here is unittest."""
        statuses = _admin_status_in_subprocess({"ADMIN_ENABLED": None})
        assert statuses == [404, 404]

    def test_admin_is_mounted_when_explicitly_enabled(self):
        """GUARD (control): the 404 above is caused by the flag, not by a broken import."""
        statuses = _admin_status_in_subprocess({"ADMIN_ENABLED": "true"})
        assert 404 not in statuses


class TestMetricsCardinality:
    def test_unmatched_paths_collapse_into_one_label(self, client):
        random_segments = [uuid.uuid4().hex for _ in range(5)]
        for segment in random_segments:
            assert client.get(f"/zz-{segment}").status_code == 404

        body = client.get("/metrics").text

        for segment in random_segments:
            assert segment not in body, "raw 404 path leaked into a metric label"
        assert 'endpoint="unmatched"' in body

    def test_matched_route_uses_its_path_template(self, client):
        """GUARD: real routes keep a bounded, meaningful label."""
        client.post("/auth/login", data={"username": "ghost", "password": "x" * 10})
        body = client.get("/metrics").text
        assert 'endpoint="/auth/login"' in body

    def test_unknown_http_method_collapses_into_the_other_label(self, client):
        """NEW-7: the method label comes from the client, so it must be bounded."""
        invented = "ZZ" + uuid.uuid4().hex[:12].upper()

        client.request(invented, "/")
        body = client.get("/metrics").text

        assert invented not in body, "client-chosen method leaked into a metric label"
        assert 'method="other"' in body

    def test_standard_methods_keep_their_label(self, client):
        """GUARD: the bounded set still distinguishes the real methods."""
        client.get("/")
        client.post("/auth/login", data={"username": "ghost", "password": "x" * 10})
        body = client.get("/metrics").text
        assert 'method="GET"' in body
        assert 'method="POST"' in body


class TestPasswordHashSettings:
    """NEW-7: Argon2 parameters need OWASP lower bounds (t>=2, m>=19456 KiB, p>=1)."""

    @pytest.mark.parametrize("env", ["unittest", "prod"])
    @pytest.mark.parametrize(
        "override",
        [
            {"ARGON2_TIME_COST": 1},
            {"ARGON2_TIME_COST": 0},
            {"ARGON2_MEMORY_KIB": 19455},
            {"ARGON2_PARALLELISM": 0},
            {"PASSWORD_HASH_CONCURRENCY": 0},
        ],
        ids=lambda o: "-".join(f"{k}={v}" for k, v in o.items()),
    )
    def test_value_below_the_bound_is_rejected(self, env, override):
        with pytest.raises(ValueError):
            _settings(env, ADMIN_ENABLED=True, **STRONG, **override)

    def test_owasp_minimum_is_accepted(self):
        """GUARD (control): the rejections above are caused by the bound only."""
        s = _settings(
            "unittest",
            ARGON2_TIME_COST=2,
            ARGON2_MEMORY_KIB=19456,
            ARGON2_PARALLELISM=1,
            PASSWORD_HASH_CONCURRENCY=1,
        )
        assert (s.ARGON2_TIME_COST, s.ARGON2_MEMORY_KIB, s.ARGON2_PARALLELISM) == (
            2,
            19456,
            1,
        )

    def test_defaults_are_accepted(self):
        """GUARD: the shipped defaults must satisfy their own bounds."""
        _settings("unittest")


class TestHarness:
    def test_app_uses_the_redis_container_not_localhost_6379(self, client):
        """Known issue 1: CACHE_HOST/CACHE_PORT must point at the container."""
        kwargs = client.app.state.cache.connection_pool.connection_kwargs
        assert kwargs["port"] == int(os.environ["CACHE_PORT"])
        assert kwargs["port"] != 6379

    def test_taskiq_broker_uses_the_redis_container(self):
        from app.common.taskiq import broker  # noqa: F401  (import for side effect check)
        from app.common.config import get_settings

        settings = get_settings()
        assert settings.TASKIQ_BROKER_URL == os.environ["TASKIQ_BROKER_URL"]
        assert (
            settings.TASKIQ_RESULT_BACKEND_URL
            == os.environ["TASKIQ_RESULT_BACKEND_URL"]
        )

    def test_settings_object_is_the_unittest_one(self):
        """app.common.taskiq must not replace the settings with the dev ones."""
        from app.common.config import get_settings

        assert get_settings().ENVIRONMENT.value == "unittest"

    def test_flush_redis_fixture_empties_the_container(self, flush_redis):
        import redis as redis_sync

        r = redis_sync.Redis(port=int(os.environ["CACHE_PORT"]), decode_responses=True)
        try:
            r.set("harness:marker", "1")
            assert r.get("harness:marker") == "1"
            flush_redis()
            assert r.get("harness:marker") is None
        finally:
            r.close()
