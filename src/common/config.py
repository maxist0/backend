# src/common/config.py
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Public code defaults. They are fine for local development and the test suite,
# but the startup guard (Settings._refuse_insecure_defaults) refuses them in
# every other environment.
_DEFAULT_JWT_SECRET_KEY = "your-super-secret-jwt-key-change-in-production"
_DEFAULT_ADMIN_PASSWORD = "admin"
_DEFAULT_ADMIN_SECRET_KEY = "admin-secret-key-change-in-production"
# RFC 7518 section 3.2: an HS256 key must have at least 256 bits.
_MIN_JWT_SECRET_LENGTH = 32


class Environment(str, Enum):
    DEV = "dev"
    TEST = "test"
    STAGE = "stage"
    PROD = "prod"
    UNITTEST = "unittest"


class Settings(BaseSettings):
    # Environment
    ENVIRONMENT: Environment = Environment.DEV

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/api"

    # Connection pool
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 30  # seconds to wait for a connection
    DB_POOL_RECYCLE: int = 1800  # recycle connections after 30 min

    # Cache
    CACHE_HOST: str = "localhost"
    CACHE_PORT: int = 6379
    CACHE_DB: int = 0
    CACHE_PASSWORD: Optional[str] = None
    CACHE_MAX_CONNECTIONS: int = 10
    ROUTE_CACHE_ENABLED: bool = True
    ROUTE_CACHE_TTL: str = "2m"
    ROUTE_CACHE_TIME_CONDITION: str = "50ms"

    # Taskiq (DB 1 = broker queue, DB 2 = result backend, keeps app cache on DB 0)
    TASKIQ_BROKER_URL: str = "redis://localhost:6379/1"
    TASKIQ_RESULT_BACKEND_URL: str = "redis://localhost:6379/2"
    TASKIQ_WORKERS: int = 2
    TASKIQ_METRICS_PORT: int = 9000
    TASKIQ_RETRY_COUNT: int = 3
    TASKIQ_RETRY_DELAY_SECONDS: int = 60

    # JWT Authentication (the algorithm is pinned in code: src/utils/jwt_utils.py)
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

    # Logging
    LOG_LEVEL: str = "INFO"

    # Access Logging
    ACCESS_LOG_ENABLED: bool = True

    # OTLP log export (Grafana Alloy / OpenTelemetry Collector)
    OTLP_LOGS_ENABLED: bool = False
    OTLP_ENDPOINT: str = "http://localhost:4318"  # base URL (no path)
    OTLP_SERVICE_NAME: str = "api"
    OTLP_HEADERS: str = ""  # comma-separated key=value pairs

    # Mail Server
    MAIL_USERNAME: Optional[str] = None
    MAIL_PASSWORD: Optional[str] = None
    MAIL_FROM: Optional[str] = None
    MAIL_PORT: int = 587
    MAIL_SERVER: Optional[str] = None
    MAIL_STARTTLS: bool = True

    # Admin panel. ADMIN_ENABLED unset means: enabled only in dev.
    ADMIN_ENABLED: bool | None = None
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: SecretStr = SecretStr(_DEFAULT_ADMIN_PASSWORD)
    ADMIN_SECRET_KEY: SecretStr = SecretStr(_DEFAULT_ADMIN_SECRET_KEY)

    # Metrics
    METRICS_ENABLED: bool = True

    # App
    DEBUG: bool = False
    APP_NAME: str = "api"
    APP_DESCRIPTION: str = "API Description"
    APP_VERSION: str = "0.1.0"
    FRONTEND_URL: str = (
        "http://localhost:3000"  # used for email links; override in production
    )

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

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

    @model_validator(mode="after")
    def _refuse_insecure_defaults(self) -> Self:
        """Startup guard: no public default secrets outside dev and unittest.

        Admin secrets are only checked when the admin panel is enabled, because
        they are not used otherwise.
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
        if self.admin_enabled:
            if self.ADMIN_PASSWORD.get_secret_value() == _DEFAULT_ADMIN_PASSWORD:
                problems.append("ADMIN_PASSWORD is the public default")
            if self.ADMIN_SECRET_KEY.get_secret_value() == _DEFAULT_ADMIN_SECRET_KEY:
                problems.append("ADMIN_SECRET_KEY is the public default")

        if problems:
            raise ValueError(
                f"Refusing to start with ENVIRONMENT={self.ENVIRONMENT.value}: "
                + "; ".join(problems)
                + ". Set strong values via environment variables."
            )
        return self

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Validation errors must not echo input values: they contain secrets.
        hide_input_in_errors=True,
    )


def get_env_file(env: str | None) -> list[str]:
    """Get env files to load based on startup mode.

    Only local development reads from .env.local. All other modes rely on
    environment variables already injected by the runtime.
    """
    if env != Environment.DEV.value:
        return []

    local_env = Path(".env.local")
    return [str(local_env)] if local_env.exists() else []


@lru_cache
def get_settings(env: str | None = None) -> Settings:
    """
    Get settings for the specified environment.

    - env="dev": reads .env.local
    - env=None or any other mode: expects injected environment variables
    """
    env_files = get_env_file(env)

    class _Settings(Settings):
        model_config = SettingsConfigDict(
            env_file=tuple(env_files),
            env_file_encoding="utf-8",
            extra="ignore",
            hide_input_in_errors=True,
        )

    if env is None:
        return _Settings()

    return _Settings(ENVIRONMENT=Environment(env))


def init_settings(env: str | None = None) -> Settings:
    """Initialize settings with the given environment."""
    global settings
    settings = get_settings(env)
    return settings


# Global settings instance - will be set by app.py
settings: Settings = None  # type: ignore
