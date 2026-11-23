"""Startup guard, admin opt-in and Argon2 bounds, all decided at `Settings` time.

* `Settings(...)` raises (`ValueError`, pydantic `ValidationError` or
  `RuntimeError`) when ENVIRONMENT is not dev/unittest and JWT_SECRET_KEY,
  ADMIN_PASSWORD or ADMIN_SECRET_KEY is still the code default, or
  JWT_SECRET_KEY (or, with the admin enabled, an admin secret) has fewer
  than 32 characters.
* JWT_SECRET_KEY, ADMIN_PASSWORD and ADMIN_SECRET_KEY are `SecretStr`.
* ADMIN_ENABLED=false (or unset outside dev) leaves `/admin` unmounted (404).
"""

import os
import re
import subprocess
import sys

import pytest

from tests.helpers import BACKEND_DIR, SettingsFactory

STRONG = {
    "JWT_SECRET_KEY": "k" * 48,
    "ADMIN_PASSWORD": "a-long-random-admin-password-1234",
    "ADMIN_SECRET_KEY": "s" * 48,
    "FRONTEND_URL": "https://app.example.com",
}
DEFAULTS = {
    "JWT_SECRET_KEY": "your-super-secret-jwt-key-change-in-production",
    "ADMIN_PASSWORD": "admin",
    "ADMIN_SECRET_KEY": "admin-secret-key-change-in-production",
}


class TestStartupGuard:
    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    @pytest.mark.parametrize(
        "unset", ["JWT_SECRET_KEY", "ADMIN_PASSWORD", "ADMIN_SECRET_KEY"]
    )
    def test_default_secret_outside_dev_refuses_to_start(
        self, settings_factory: SettingsFactory, env: str, unset: str
    ) -> None:
        kwargs = {
            k: v for k, v in STRONG.items() if k != unset
        }  # `unset` keeps its default

        with pytest.raises((ValueError, RuntimeError)):
            settings_factory(ENVIRONMENT=env, ADMIN_ENABLED=True, **kwargs)

    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    @pytest.mark.parametrize("admin_enabled", [False, None], ids=["false", "unset"])
    def test_admin_disabled_with_default_admin_secrets_starts(
        self,
        settings_factory: SettingsFactory,
        env: str,
        *,
        admin_enabled: bool | None,
    ) -> None:
        """Admin secrets are only checked when the admin is enabled."""
        # An explicit None overrides the process-wide ADMIN_ENABLED=true of conftest.
        s = settings_factory(
            ENVIRONMENT=env,
            JWT_SECRET_KEY=STRONG["JWT_SECRET_KEY"],
            FRONTEND_URL=STRONG["FRONTEND_URL"],
            ADMIN_ENABLED=admin_enabled,
        )

        assert s.admin_enabled is False

    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    def test_admin_disabled_still_requires_strong_jwt_secret(
        self, settings_factory: SettingsFactory, env: str
    ) -> None:
        with pytest.raises((ValueError, RuntimeError)):
            settings_factory(ENVIRONMENT=env, ADMIN_ENABLED=False)

    @pytest.mark.parametrize(
        "unset", ["JWT_SECRET_KEY", "ADMIN_PASSWORD", "ADMIN_SECRET_KEY"]
    )
    def test_explicitly_passing_the_default_value_is_also_refused(
        self, settings_factory: SettingsFactory, unset: str
    ) -> None:
        kwargs = dict(STRONG, **{unset: DEFAULTS[unset]})

        with pytest.raises((ValueError, RuntimeError)):
            settings_factory(ENVIRONMENT="prod", ADMIN_ENABLED=True, **kwargs)

    def test_short_jwt_secret_outside_dev_refuses_to_start(
        self, settings_factory: SettingsFactory
    ) -> None:
        with pytest.raises((ValueError, RuntimeError)):
            settings_factory(
                ENVIRONMENT="prod", **dict(STRONG, JWT_SECRET_KEY="k" * 31)
            )

    @pytest.mark.parametrize("name", ["ADMIN_PASSWORD", "ADMIN_SECRET_KEY"])
    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    def test_short_admin_secret_with_admin_enabled_refuses_to_start(
        self, settings_factory: SettingsFactory, env: str, name: str
    ) -> None:
        with pytest.raises((ValueError, RuntimeError), match=f"{name} is shorter"):
            settings_factory(
                ENVIRONMENT=env, ADMIN_ENABLED=True, **dict(STRONG, **{name: "x" * 31})
            )

    def test_admin_secrets_of_32_characters_start(
        self, settings_factory: SettingsFactory
    ) -> None:
        s = settings_factory(
            ENVIRONMENT="prod",
            ADMIN_ENABLED=True,
            **dict(STRONG, ADMIN_PASSWORD="p" * 32, ADMIN_SECRET_KEY="s" * 32),
        )
        assert s.admin_enabled is True

    def test_prod_with_strong_secrets_starts(
        self, settings_factory: SettingsFactory
    ) -> None:
        s = settings_factory(ENVIRONMENT="prod", **STRONG)
        assert s.is_production

    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    @pytest.mark.parametrize(
        "value", ["*", "0.0.0.0/0", "::/0", "10.0.0.2, *", "10.0.0.2,::0/0"]
    )
    def test_forwarded_allow_ips_trusting_every_peer_refuses_to_start(
        self, settings_factory: SettingsFactory, env: str, value: str
    ) -> None:
        """Else every client chooses its own address in `X-Forwarded-For`."""
        with pytest.raises(ValueError, match="FORWARDED_ALLOW_IPS trusts every"):
            settings_factory(ENVIRONMENT=env, FORWARDED_ALLOW_IPS=value, **STRONG)

    @pytest.mark.parametrize(
        "value", ["127.0.0.1", "10.0.0.0/8, 172.16.0.0/12", "fd00::/8", "proxy"]
    )
    def test_forwarded_allow_ips_of_proxies_starts(
        self, settings_factory: SettingsFactory, value: str
    ) -> None:
        s = settings_factory(ENVIRONMENT="prod", FORWARDED_ALLOW_IPS=value, **STRONG)
        assert value == s.FORWARDED_ALLOW_IPS

    @pytest.mark.parametrize("env", ["dev", "unittest"])
    def test_forwarded_allow_ips_star_is_allowed_in_dev_and_unittest(
        self, settings_factory: SettingsFactory, env: str
    ) -> None:
        s = settings_factory(ENVIRONMENT=env, FORWARDED_ALLOW_IPS="*")
        assert s.FORWARDED_ALLOW_IPS == "*"

    @pytest.mark.parametrize("env", ["prod", "stage", "test"])
    @pytest.mark.parametrize(
        ("frontend_url", "problem"),
        [
            (None, "FRONTEND_URL is the development default"),
            ("http://localhost:5173", "FRONTEND_URL is the development default"),
            ("http://app.example.com", "FRONTEND_URL must use https"),
        ],
        ids=["unset", "default", "http"],
    )
    def test_frontend_url_outside_dev_must_be_https_and_not_the_default(
        self,
        settings_factory: SettingsFactory,
        env: str,
        frontend_url: str | None,
        problem: str,
    ) -> None:
        """Email links carry tokens: no dev server, no plain http."""
        kwargs = {k: v for k, v in STRONG.items() if k != "FRONTEND_URL"}
        if frontend_url is not None:
            kwargs["FRONTEND_URL"] = frontend_url

        with pytest.raises(ValueError, match=problem):
            settings_factory(ENVIRONMENT=env, ADMIN_ENABLED=False, **kwargs)

    @pytest.mark.parametrize("env", ["dev", "unittest"])
    def test_defaults_are_still_allowed_in_dev_and_unittest(
        self, settings_factory: SettingsFactory, env: str
    ) -> None:
        """Local development and the test suite keep working."""
        assert settings_factory(ENVIRONMENT=env).ENVIRONMENT.value == env

    def test_secrets_are_not_exposed_by_repr_or_str(
        self, settings_factory: SettingsFactory
    ) -> None:
        s = settings_factory(**STRONG)
        text = repr(s) + str(s)
        for name, value in STRONG.items():
            if name != "FRONTEND_URL":  # not a secret
                assert value not in text

    def test_default_secrets_are_not_exposed_by_repr(
        self, settings_factory: SettingsFactory
    ) -> None:
        text = repr(settings_factory())
        for value in (DEFAULTS["JWT_SECRET_KEY"], DEFAULTS["ADMIN_SECRET_KEY"]):
            assert value not in text


def _admin_status_in_subprocess(env_overrides: dict[str, str | None]) -> list[int]:
    """Import the real app in a fresh interpreter and request /admin routes.

    ENVIRONMENT=unittest is inherited from the test process.

    The TestClient is not entered as a context manager, so no lifespan (and no
    database) is needed: an unmounted /admin yields a plain 404.
    """
    code = (
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app, follow_redirects=False)\n"
        "login, root = c.get('/admin/login'), c.get('/admin/')\n"
        "print('STATUS', login.status_code, root.status_code)\n"
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
        check=False,
    )
    match = re.search(r"STATUS (\d+) (\d+)", proc.stdout)
    assert match, f"subprocess failed:\n{proc.stdout[-500:]}\n{proc.stderr[-1500:]}"
    return [int(match.group(1)), int(match.group(2))]


class TestAdminOptIn:
    def test_admin_is_not_mounted_when_admin_enabled_is_false(self) -> None:
        statuses = _admin_status_in_subprocess({"ADMIN_ENABLED": "false"})
        assert statuses == [404, 404]

    def test_admin_is_not_mounted_by_default_outside_dev(self) -> None:
        """ADMIN_ENABLED unset: true only in dev. ENVIRONMENT here is unittest."""
        statuses = _admin_status_in_subprocess({"ADMIN_ENABLED": None})
        assert statuses == [404, 404]

    def test_admin_is_mounted_when_explicitly_enabled(self) -> None:
        """Control: the 404 above is caused by the flag, not by a broken import."""
        statuses = _admin_status_in_subprocess({"ADMIN_ENABLED": "true"})
        assert 404 not in statuses


class TestPasswordHashSettings:
    """Argon2 parameters need OWASP lower bounds (t>=2, m>=19456 KiB, p>=1)."""

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
    def test_value_below_the_bound_is_rejected(
        self, settings_factory: SettingsFactory, env: str, override: dict[str, int]
    ) -> None:
        with pytest.raises(ValueError, match=next(iter(override))):
            settings_factory(ENVIRONMENT=env, ADMIN_ENABLED=True, **STRONG, **override)

    def test_owasp_minimum_is_accepted(self, settings_factory: SettingsFactory) -> None:
        """Control: the rejections above are caused by the bound only."""
        s = settings_factory(
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

    def test_defaults_are_accepted(self, settings_factory: SettingsFactory) -> None:
        """The shipped defaults must satisfy their own bounds."""
        settings_factory()


class TestHarness:
    def test_settings_object_is_the_unittest_one(self) -> None:
        """No imported module may replace the settings with the dev ones."""
        from app.core.config import get_settings

        assert get_settings().ENVIRONMENT.value == "unittest"
