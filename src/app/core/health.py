"""Liveness, readiness and the Prometheus endpoint (all without the API prefix).

The metrics use prometheus_client's default registry, which also carries
the process and garbage-collector collectors. One uvicorn process per
container is assumed; several worker processes in one container would need
prometheus_client's multiprocess mode.
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import APIRouter, FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Info, generate_latest
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.cache import RedisDep
from app.core.config import Settings, SettingsDep
from app.core.logging import get_logger

logger = get_logger(__name__)

METRICS_PATH = "/metrics"

APP_INFO = Info("app", "Name, version and environment of the running API.")

CheckStatus = Literal["ok", "error"]


class LivenessResponse(BaseModel):
    """Answer of `GET /health`."""

    status: Literal["ok"]


class ReadinessResponse(BaseModel):
    """Answer of `GET /health/ready`: overall status and one entry per check."""

    status: CheckStatus
    checks: dict[str, CheckStatus]


router = APIRouter(tags=["health"])


@router.get(
    "/health",
    status_code=200,
    summary="Liveness",
    description="Answers while the process serves requests. Checks no dependency.",
)
async def liveness() -> LivenessResponse:
    """Report that the process is alive."""
    return LivenessResponse(status="ok")


async def _check(
    name: str, probe: Callable[[], Awaitable[object]], seconds: float
) -> CheckStatus:
    """Run one readiness probe with a timeout; log a failure by type only."""
    try:
        async with asyncio.timeout(seconds):
            await probe()
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready" (503)
        logger.warning("health.check_failed", check=name, error_type=type(exc).__name__)
        return "error"
    return "ok"


async def _ping_database(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


@router.get(
    "/health/ready",
    status_code=200,
    summary="Readiness",
    description="Pings PostgreSQL and Redis. Answers 503 while one of them "
    "fails; `checks` names the failed dependency.",
    responses={503: {"model": ReadinessResponse, "description": "Not ready"}},
)
async def readiness(
    request: Request, response: Response, redis: RedisDep, settings: SettingsDep
) -> ReadinessResponse:
    """Report whether the database and Redis answer within the timeout."""
    engine: AsyncEngine = request.app.state.engine
    timeout = settings.READINESS_TIMEOUT_SECONDS
    checks: dict[str, CheckStatus] = {
        "database": await _check("database", lambda: _ping_database(engine), timeout),
        "redis": await _check("redis", redis.ping, timeout),  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType] - redis-py leaves `**kwargs` of ping() unannotated
    }
    ready = all(status == "ok" for status in checks.values())
    if not ready:
        response.status_code = 503
    return ReadinessResponse(status="ok" if ready else "error", checks=checks)


metrics_router = APIRouter()


@metrics_router.get(METRICS_PATH, include_in_schema=False)
async def metrics() -> Response:
    """Expose the default registry in the Prometheus text format."""
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


def include_health_routes(app: FastAPI, settings: Settings) -> None:
    """Mount the health endpoints and, with `METRICS_ENABLED`, `/metrics`."""
    app.include_router(router)
    if settings.METRICS_ENABLED:
        APP_INFO.info(
            {
                "name": settings.APP_NAME,
                "version": settings.APP_VERSION,
                "environment": settings.ENVIRONMENT.value,
            }
        )
        app.include_router(metrics_router)
