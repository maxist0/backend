"""Application factory and ASGI entrypoint (composition root of the API).

`create_app(settings)` assembles the FastAPI app (middleware, the health
and metrics routes, the routers of every module in `MODULES` under
`API_PREFIX`, the problem-details error handlers, optional admin) without
any I/O. The lifespan opens the resources (logging, DB engine, Redis, cache,
taskiq broker) and closes them again, also when startup fails half-way.
`app = create_app()` is the entrypoint for uvicorn, `fastapi run` and
`run.py`; importing this module requires `ENVIRONMENT` to be set.
"""

import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import async_shared_broker

from app.core.admin import create_admin
from app.core.cache import close_cache, close_redis, open_redis, setup_cache
from app.core.config import Settings, get_settings
from app.core.database import (
    build_engine,
    close_db,
    create_session_factory,
    ensure_supported_event_loop,
    init_db,
)
from app.core.errors import register_error_handlers
from app.core.health import METRICS_PATH, include_health_routes
from app.core.idempotency import REPLAYED_HEADER, register_idempotency
from app.core.logging import get_logger, setup_logging
from app.core.middleware import RequestMiddleware
from app.core.rate_limiting import warn_if_disabled
from app.core.tasks import build_broker
from app.modules import MODULES

logger = get_logger(__name__)


def _unbind_session_factory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Drop the engine binding so no code uses a disposed engine."""
    session_factory.configure(bind=None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Open the app's resources on startup and close them on shutdown.

    Every resource registers its cleanup on an `AsyncExitStack` right after
    it is created, so a failure later in the startup closes everything that
    was already open, in reverse order, before the error propagates.
    """
    settings: Settings = app.state.settings
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory

    setup_logging(settings)
    logger.info(
        "app.startup",
        pid=os.getpid(),
        cwd=str(Path.cwd()),
        app_name=app.title,
        app_version=app.version,
        app_description=app.description,
        environment=settings.ENVIRONMENT.value.upper(),
        host=settings.HOST,
        port=settings.PORT,
    )
    warn_if_disabled(settings)

    async with AsyncExitStack() as stack:
        # Database: loop check (Windows), engine, connectivity check, bind
        # the session factory.
        ensure_supported_event_loop()
        engine = build_engine(settings)
        stack.push_async_callback(close_db, engine)
        await init_db(engine, settings)
        session_factory.configure(bind=engine)
        stack.callback(_unbind_session_factory, session_factory)
        app.state.engine = engine

        # Redis client (RedisDep) and the @cached cache on the same server.
        app.state.redis = await open_redis(settings)
        stack.push_async_callback(close_redis, app.state.redis)
        setup_cache(settings)
        stack.push_async_callback(close_cache)

        # Taskiq broker for enqueueing. Shared tasks (`app.core.task`) send
        # to the default broker; a later lifespan replaces it.
        broker = build_broker(settings)
        stack.push_async_callback(broker.shutdown)
        await broker.startup()
        async_shared_broker.default_broker(broker)
        app.state.broker = broker

        yield

        logger.info("app.shutdown")

    logger.info("app.shutdown_complete")


class RootResponse(BaseModel):
    """Entry points of the API, answered by `GET /`."""

    message: str
    docs_url: str | None
    metrics_url: str | None
    auth_url: str


def _add_root_route(app: FastAPI, settings: Settings) -> None:
    """Register `GET /`, which lists the entry points for these settings."""

    @app.get("/", summary="API entry points")
    async def root() -> RootResponse:
        """List the entry points of the API."""
        return RootResponse(
            message="Welcome to the FastAPI application with JWT authentication!",
            docs_url="/docs" if not settings.is_production else None,
            metrics_url=METRICS_PATH if settings.METRICS_ENABLED else None,
            auth_url=f"{settings.API_PREFIX}/auth",
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the FastAPI application; performs no I/O.

    Args:
        settings: Defaults to `get_settings()`. The given settings drive the
            app assembly and the lifespan resources; request-time code reads
            `get_settings()` (`SettingsDep`).

    Returns:
        The app with every module router of `MODULES` mounted under
        `settings.API_PREFIX`.
    """
    if settings is None:
        settings = get_settings()

    app = FastAPI(
        title=settings.APP_NAME,
        description=settings.APP_DESCRIPTION,
        version=settings.APP_VERSION,
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
        # FastAPI >= 0.142 emits OpenTelemetry spans, metrics and log records
        # once a global provider exists; its log records carry exception
        # messages and stack traces (personal data), bypassing
        # log_unexpected_error. Off until tracing is decided.
        telemetry={"tracing": False, "metrics": False, "logs": False},
    )
    app.state.settings = settings
    # Unbound until the lifespan has created the engine.
    session_factory = create_session_factory()
    app.state.session_factory = session_factory

    # Admin panel (opt-in: ADMIN_ENABLED, unset means dev only)
    if settings.admin_enabled:
        views = [view for module in MODULES for view in module.admin_views]
        create_admin(app, settings, session_factory, views)

    register_error_handlers(app)
    register_idempotency(app, settings)

    # The last middleware added runs first. CORS is outside the request
    # middleware so the 500 that the request middleware renders still gets
    # the CORS headers; Starlette's ServerErrorMiddleware wraps both.
    app.add_middleware(
        RequestMiddleware,
        api_prefix=settings.API_PREFIX,
        access_log=settings.ACCESS_LOG_ENABLED,
        metrics=settings.METRICS_ENABLED,
        max_body_bytes=settings.MAX_REQUEST_BODY_BYTES,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # Without this a browser on another origin cannot read them: the
        # wait after a 429, the id to quote in a support request and the
        # mark of a replayed response.
        expose_headers=["Retry-After", "X-Request-ID", REPLAYED_HEADER],
    )

    include_health_routes(app, settings)
    for module in MODULES:
        app.include_router(module.router, prefix=settings.API_PREFIX)
    _add_root_route(app, settings)

    return app


# ASGI entrypoint ([tool.fastapi] entrypoint, uvicorn "app.main:app").
app = create_app()
