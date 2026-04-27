# src/common/config.py
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    CACHE_KEY_PREFIX: str = "fastapi-cache"

    # Taskiq (DB 1 = broker queue, DB 2 = result backend, keeps app cache on DB 0)
    TASKIQ_BROKER_URL: str = "redis://localhost:6379/1"
    TASKIQ_RESULT_BACKEND_URL: str = "redis://localhost:6379/2"
    TASKIQ_WORKERS: int = 2
    TASKIQ_METRICS_PORT: int = 9000
    TASKIQ_RETRY_COUNT: int = 3
    TASKIQ_RETRY_DELAY_SECONDS: int = 60

    # JWT Authentication
    JWT_SECRET_KEY: str = "your-super-secret-jwt-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # Password hashing
    BCRYPT_ROUNDS: int = 12

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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
