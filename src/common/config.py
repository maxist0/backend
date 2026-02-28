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

    # Metrics
    METRICS_ENABLED: bool = True

    # App
    DEBUG: bool = False
    APP_NAME: str = "api"
    APP_DESCRIPTION: str = "API Description"
    APP_VERSION: str = "0.1.0"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    @property
    def is_production(self) -> bool:
        """Check if the application is running in production mode."""
        return self.ENVIRONMENT == Environment.PROD

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


def get_env_file(env: str) -> list[str]:
    """Get the env files to load based on environment."""
    base_env = Path(".env")
    env_specific = Path(f".env.{env}")

    files = []
    if base_env.exists():
        files.append(str(base_env))
    if env_specific.exists():
        files.append(str(env_specific))

    return files


@lru_cache
def get_settings(env: str) -> Settings:
    """
    Get settings for the specified environment.
    Environment-specific file (.env.{env}) overrides base .env
    """
    env_files = get_env_file(env)
    print(f"Loading settings for environment '{env}' from files: {env_files}")

    class _Settings(Settings):
        model_config = SettingsConfigDict(env_file=env_files or ())

    return _Settings(ENVIRONMENT=Environment(env))


def init_settings(env: str) -> Settings:
    """Initialize settings with the given environment."""
    global settings
    settings = get_settings(env)
    return settings


# Global settings instance - will be set by app.py
settings: Settings = None # type: ignore