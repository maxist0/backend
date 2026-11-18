"""Application settings.

`get_settings()` is the only way to obtain the process settings. It reads
the environment once (cached) and requires `ENVIRONMENT` to be set
explicitly; there is no silent fallback to `dev`. Only `dev` additionally
reads `.env.local`; real environment variables always win over that file.
Routes receive the settings through `SettingsDep`.
"""

import ipaddress
import os
import re
from enum import Enum
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import Annotated, Self

from fastapi import Depends
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Public code defaults. They are fine for local development and the test suite,
# but the startup guard (Settings._refuse_insecure_defaults) refuses them in
# every other environment.
_DEFAULT_JWT_SECRET_KEY = "your-super-secret-jwt-key-change-in-production"
_DEFAULT_ADMIN_PASSWORD = "admin"
_DEFAULT_ADMIN_SECRET_KEY = "admin-secret-key-change-in-production"
# RFC 7518 section 3.2: an HS256 key must have at least 256 bits.
_MIN_JWT_SECRET_LENGTH = 32
# The admin password guards every account; ADMIN_SECRET_KEY signs the admin
# session (HMAC), so it gets the same floor as the JWT key.
_MIN_ADMIN_SECRET_LENGTH = 32

# [project].name in pyproject.toml; [project].version is the only version source.
_DISTRIBUTION_NAME = "api"
# PEP 440 local version: marks a build that runs without installed metadata.
_UNKNOWN_VERSION = "0.0.0+unknown"
# A browser `Origin` value: scheme and host with an optional port, no path.
_ORIGIN = re.compile(r"https?://[^/\s]+")
# An absolute URL path of non-empty segments of RFC 3986 `pchar`s: no scheme,
# no host (`//host` has an empty first segment), no query or fragment.
_FRONTEND_PATH = r"^(/[A-Za-z0-9._~!$&'()*+,;=:@%-]+)+$"
# The base of the email links: http(s), a host name (ASCII, IDNs in punycode)
# or a bracketed IPv6 address, an optional port and an optional path of RFC
# 3986 `pchar`s. Nothing else: no userinfo (`user@host` disguises the real
# host), no query or fragment (the link appends `?token=`).
_FRONTEND_URL = (
    r"^https?://([A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(:[0-9]{1,5})?"
    r"(/[A-Za-z0-9._~!$&'()*+,;=:@%-]*)*$"
)
_DEFAULT_FRONTEND_URL = "http://localhost:5173"
# RFC 1123 hostname: dot-separated labels of letters, digits and inner hyphens.
_HOSTNAME = re.compile(
    r"(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)


def _installed_version() -> str:
    """Return the installed distribution version, or a visible sentinel."""
    try:
        return distribution_version(_DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _UNKNOWN_VERSION


# The only supported database driver: psycopg 3 (async).
DATABASE_URL_SCHEME = "postgresql+psycopg"
# Generic PostgreSQL URL schemes (libpq, Heroku-style) that mean the same server.
_GENERIC_POSTGRES_SCHEMES = frozenset({"postgresql", "postgres"})


def normalize_database_url(url: str) -> str:
    """Return *url* with the `postgresql+psycopg` scheme.

    `postgresql://` and `postgres://` are rewritten (they name no driver,
    so the meaning does not change). Any other driver is refused instead of
    silently replaced: an explicit `+asyncpg` or `+psycopg2` in a deployment
    is a stale configuration someone should see. Messages contain only the
    scheme, never the URL (it holds the password).
    """
    url = url.strip()
    scheme, separator, rest = url.partition("://")
    if not separator:
        raise ValueError(f"DATABASE_URL must start with {DATABASE_URL_SCHEME}://")
    scheme = scheme.lower()
    if scheme == DATABASE_URL_SCHEME or scheme in _GENERIC_POSTGRES_SCHEMES:
        return f"{DATABASE_URL_SCHEME}://{rest}"
    if scheme == "postgresql+asyncpg":
        raise ValueError(
            "DATABASE_URL uses the asyncpg driver, which is no longer installed. "
            f"Change the scheme to {DATABASE_URL_SCHEME}:// (psycopg 3)."
        )
    raise ValueError(
        f"DATABASE_URL scheme {scheme!r} is not supported; "
        f"use {DATABASE_URL_SCHEME}:// (psycopg 3)."
    )


class ConfigurationError(RuntimeError):
    """The process environment does not allow building the settings.

    Messages never contain secret values.
    """


class Environment(str, Enum):
    DEV = "dev"
    TEST = "test"
    STAGE = "stage"
    PROD = "prod"
    UNITTEST = "unittest"


class Settings(BaseSettings):
    # Environment: required, never defaulted (no silent fallback to dev).
    ENVIRONMENT: Environment

    # Database (psycopg 3; postgresql:// and postgres:// are normalised)
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/api"
    # Seconds to establish one connection (psycopg's own default is 130).
    # libpq treats values below 2 as 2.
    DB_CONNECT_TIMEOUT: int = Field(default=10, ge=2)

    # Connection pool
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30  # seconds to wait for a connection
    DB_POOL_RECYCLE: int = 1800  # recycle connections after 30 min

    # Cache
    CACHE_HOST: str = "localhost"
    CACHE_PORT: int = 6379
    CACHE_DB: int = 0
    CACHE_PASSWORD: str | None = None
    CACHE_MAX_CONNECTIONS: int = 10

    # Taskiq (DB 1 = broker queue, DB 2 = result backend, keeps app cache on DB 0)
    TASKIQ_BROKER_URL: str = "redis://localhost:6379/1"
    TASKIQ_RESULT_BACKEND_URL: str = "redis://localhost:6379/2"
    # Worker processes of `taskiq worker` (one on Windows dev, see run.py).
    TASKIQ_WORKERS: int = Field(default=2, ge=1, le=64)
    # Address and port of the worker's Prometheus endpoint (worker processes
    # only). Loopback by default; a container sets 0.0.0.0 so the scraper on
    # the monitoring network can reach it.
    TASKIQ_METRICS_HOST: str = "127.0.0.1"
    TASKIQ_METRICS_PORT: int = Field(default=9000, ge=1, le=65_535)
    # Retries of a task that fails with TransientTaskError, after the first
    # run; each waits TASKIQ_RETRY_DELAY_SECONDS (plus up to 1 s jitter and
    # the scheduler's update interval).
    TASKIQ_RETRY_COUNT: int = Field(default=3, ge=0, le=10)
    # At least 30 s: a shorter delay retries into the same outage and turns
    # a brief SMTP or network problem into a burst of failed attempts.
    TASKIQ_RETRY_DELAY_SECONDS: int = Field(default=60, ge=30, le=3_600)

    # JWT Authentication (the algorithm is pinned in code: app.core.security)
    JWT_SECRET_KEY: SecretStr = SecretStr(_DEFAULT_JWT_SECRET_KEY)
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Password hashing (Argon2id). Defaults equal argon2-cffi's defaults
    # (RFC 9106 "second recommended option"), so existing hashes stay valid.
    # Lower bounds = OWASP Password Storage Cheat Sheet minimum for Argon2id
    # (m=19 MiB, t=2, p=1); enforced in every environment.
    ARGON2_TIME_COST: int = Field(default=3, ge=2)
    ARGON2_MEMORY_KIB: int = Field(default=65536, ge=19456)
    ARGON2_PARALLELISM: int = Field(default=4, ge=1)
    # Max. concurrent hash/verify operations per process (each needs
    # ARGON2_MEMORY_KIB of RAM); further requests wait for a free slot.
    PASSWORD_HASH_CONCURRENCY: int = Field(default=4, ge=1)

    # Login lockout: this many failed password logins lock the account for
    # LOGIN_LOCKOUT_SECONDS. NIST SP 800-63B-4 (3.2.2) allows at most 100
    # consecutive failures; a lock longer than a day hands anyone who knows a
    # username a long denial of service.
    LOGIN_MAX_ATTEMPTS: int = Field(default=5, ge=1, le=100)
    LOGIN_LOCKOUT_SECONDS: int = Field(default=900, ge=1, le=86_400)

    # Days that ended refresh sessions and closed email tokens are kept
    # before the nightly cleanup task deletes them. They hold IP addresses,
    # user agents and old or requested email addresses, so they are not
    # kept for good; at least a day, so a recent incident can be inspected.
    AUTH_CLEANUP_RETENTION_DAYS: int = Field(default=30, ge=1, le=365)

    # Logging (stdout only)
    LOG_LEVEL: str = "INFO"
    ACCESS_LOG_ENABLED: bool = True

    # Mail Server
    MAIL_USERNAME: str | None = None
    MAIL_PASSWORD: SecretStr | None = None
    MAIL_FROM: str | None = None
    MAIL_PORT: int = 587
    MAIL_SERVER: str | None = None
    MAIL_STARTTLS: bool = True

    # Admin panel. ADMIN_ENABLED unset means: enabled only in dev.
    ADMIN_ENABLED: bool | None = None
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: SecretStr = SecretStr(_DEFAULT_ADMIN_PASSWORD)
    ADMIN_SECRET_KEY: SecretStr = SecretStr(_DEFAULT_ADMIN_SECRET_KEY)

    # Metrics and health
    METRICS_ENABLED: bool = True
    # Per dependency check of `GET /health/ready`.
    READINESS_TIMEOUT_SECONDS: float = Field(default=2.0, gt=0, le=30)

    # App
    APP_NAME: str = "api"
    APP_DESCRIPTION: str = "API Description"
    APP_VERSION: str = Field(default_factory=_installed_version)
    # Path prefix of every module router (API version). `/`, `/metrics` and
    # the health endpoints stay unprefixed. Empty, or segments without a
    # trailing slash.
    API_PREFIX: str = Field(default="/api/v1", pattern=r"^(/[A-Za-z0-9._~-]+)*$")
    # Base of the links in emails (scheme, host, optional port and path);
    # the default is the frontend's dev server. The startup guard requires an
    # https value other than the default outside dev and unittest.
    FRONTEND_URL: str = Field(default=_DEFAULT_FRONTEND_URL, pattern=_FRONTEND_URL)
    # Frontend pages the email links open, appended to FRONTEND_URL: absolute
    # paths only, so a value cannot point the link at another host.
    FRONTEND_VERIFY_EMAIL_PATH: str = Field(
        default="/user/email-bestaetigen", pattern=_FRONTEND_PATH
    )
    FRONTEND_RESET_PASSWORD_PATH: str = Field(
        default="/user/passwort-zuruecksetzen", pattern=_FRONTEND_PATH
    )

    # Browser origins allowed to call the API with credentials (CORS). A JSON
    # list in the environment: CORS_ORIGINS='["https://app.example.com"]'.
    CORS_ORIGINS: list[str] = ["http://localhost:5173"]

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    # Proxy addresses whose X-Forwarded-For/-Proto uvicorn trusts for the
    # client address and scheme (comma-separated). Set it to the reverse
    # proxy's addresses, otherwise every client IP is the proxy's.
    FORWARDED_ALLOW_IPS: str = "127.0.0.1"

    @property
    def is_development(self) -> bool:
        """Check if the application is running in development mode."""
        return self.ENVIRONMENT == Environment.DEV

    @property
    def is_production(self) -> bool:
        """Check if the application is running in production mode."""
        return self.ENVIRONMENT == Environment.PROD

    @property
    def admin_enabled(self) -> bool:
        """Whether the admin panel is mounted (opt-in outside dev)."""
        if self.ADMIN_ENABLED is None:
            return self.is_development
        return self.ADMIN_ENABLED

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    @field_validator("TASKIQ_METRICS_HOST")
    @classmethod
    def _require_host(cls, value: str) -> str:
        """Accept an IP address or a hostname, nothing else."""
        try:
            ipaddress.ip_address(value)
        except ValueError:
            if not _HOSTNAME.fullmatch(value):
                raise ValueError(
                    f"TASKIQ_METRICS_HOST {value!r} is neither an IP address "
                    "nor a hostname."
                ) from None
        return value

    @field_validator("CORS_ORIGINS")
    @classmethod
    def _require_explicit_origins(cls, origins: list[str]) -> list[str]:
        """Refuse `*` and anything that is not an origin, in every environment.

        Credentials are allowed, and with `*` Starlette answers every origin
        with credentials, so any website could call the API as the user.
        """
        for origin in origins:
            if origin == "*":
                raise ValueError(
                    "CORS_ORIGINS must not contain '*': credentials are allowed, "
                    "so list the allowed origins explicitly."
                )
            if not _ORIGIN.fullmatch(origin):
                raise ValueError(
                    f"CORS_ORIGINS entry {origin!r} is not an origin "
                    "(scheme://host[:port], no path or trailing slash)."
                )
        return origins

    @model_validator(mode="after")
    def _refuse_insecure_defaults(self) -> Self:
        """Startup guard: no public defaults outside dev and unittest.

        Covers the secrets and `FRONTEND_URL`, which must be an https URL
        other than the dev server: email links carry tokens. Admin secrets
        are only checked when the admin panel is enabled, because they are
        not used otherwise.
        """
        if self.ENVIRONMENT in (Environment.DEV, Environment.UNITTEST):
            return self

        problems: list[str] = []
        jwt_secret = self.JWT_SECRET_KEY.get_secret_value()
        if jwt_secret == _DEFAULT_JWT_SECRET_KEY:
            problems.append("JWT_SECRET_KEY is the public default")
        elif len(jwt_secret) < _MIN_JWT_SECRET_LENGTH:
            problems.append(
                f"JWT_SECRET_KEY is shorter than {_MIN_JWT_SECRET_LENGTH} characters"
            )
        if self.FRONTEND_URL == _DEFAULT_FRONTEND_URL:
            problems.append("FRONTEND_URL is the development default")
        elif not self.FRONTEND_URL.startswith("https://"):
            problems.append("FRONTEND_URL must use https")
        if self.admin_enabled:
            admin_secrets = (
                ("ADMIN_PASSWORD", self.ADMIN_PASSWORD, _DEFAULT_ADMIN_PASSWORD),
                ("ADMIN_SECRET_KEY", self.ADMIN_SECRET_KEY, _DEFAULT_ADMIN_SECRET_KEY),
            )
            for name, secret, default in admin_secrets:
                value = secret.get_secret_value()
                if value == default:
                    problems.append(f"{name} is the public default")
                elif len(value) < _MIN_ADMIN_SECRET_LENGTH:
                    problems.append(
                        f"{name} is shorter than {_MIN_ADMIN_SECRET_LENGTH} characters"
                    )

        if problems:
            raise ValueError(
                f"Refusing to start with ENVIRONMENT={self.ENVIRONMENT.value}: "
                + "; ".join(problems)
                + ". Set strong values via environment variables."
            )
        return self

    model_config = SettingsConfigDict(
        # No dotenv file by default: `.env` is a template with empty values.
        # get_settings() adds .env.local for ENVIRONMENT=dev only.
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors must not echo input values: they contain secrets.
        hide_input_in_errors=True,
    )


# Read by get_settings() for ENVIRONMENT=dev only (relative to the working
# directory, i.e. backend/ when started via run.py or uv run).
LOCAL_ENV_FILE = Path(".env.local")
_ENVIRONMENT_VARIABLE = "ENVIRONMENT"


def environment_from_os() -> Environment:
    """Return `ENVIRONMENT` from the process environment.

    Raises ConfigurationError when it is missing, empty or unknown. It is
    read from the process environment only, because it decides whether
    `.env.local` is read at all.
    """
    raw = os.environ.get(_ENVIRONMENT_VARIABLE, "").strip()
    allowed = ", ".join(e.value for e in Environment)
    if not raw:
        raise ConfigurationError(
            f"ENVIRONMENT is not set. Set it to one of: {allowed} "
            "(for local development: 'python run.py dev')."
        )
    try:
        return Environment(raw)
    except ValueError:
        raise ConfigurationError(
            f"ENVIRONMENT={raw!r} is not valid. Expected one of: {allowed}."
        ) from None


class _DevSettings(Settings):
    """Settings for ENVIRONMENT=dev: additionally read `.env.local`.

    pydantic-settings skips a missing file. All other environments rely on
    variables injected by the runtime (Docker, Swarm, CI, the test harness).
    """

    model_config = SettingsConfigDict(env_file=LOCAL_ENV_FILE)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings, built once from the environment.

    Priority (pydantic-settings): environment variables > `.env.local`
    (dev only) > code defaults. `ENVIRONMENT` itself comes from the
    process environment only. Tests that change the environment call
    `get_settings.cache_clear()`.
    """
    environment = environment_from_os()
    settings_class = _DevSettings if environment is Environment.DEV else Settings
    return settings_class(ENVIRONMENT=environment)


# Tests override `get_settings` via `app.dependency_overrides` or
# `get_settings.cache_clear()`.
SettingsDep = Annotated[Settings, Depends(get_settings)]
