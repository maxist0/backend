"""Application factory and ASGI entrypoint (composition root of the API).

`create_app(settings)` assembles the FastAPI app (middleware, the routers of
every module in `MODULES`, exception handlers, optional admin) without any
I/O. The lifespan opens the resources (logging, metrics, DB engine, Redis,
route cache, taskiq broker) and closes them again, also when startup fails
half-way. `app = create_app()` is the entrypoint for uvicorn, `fastapi run`
and `run.py`; importing this module requires `ENVIRONMENT` to be set.
"""

import os
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime

from cashews import cache
from cashews.contrib.fastapi import (
    CacheEtagMiddleware,
    CacheRequestControlMiddleware,
    cache_control_ttl,
)
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from taskiq import async_shared_broker

from app.core.admin import create_admin
from app.core.cache import (
    close_cache_pool,
    close_route_cache,
    init_cache_pool,
    init_route_cache,
)
from app.core.config import Settings, get_settings
from app.core.database import (
    build_engine,
    close_db,
    create_session_factory,
    ensure_supported_event_loop,
    init_db,
)
from app.core.errors import (
    AuthenticationError,
    authentication_exception_handler,
    generic_exception_handler,
    validation_exception_handler,
)
from app.core.logging import get_logger, setup_logging
from app.core.metrics import init_metrics
from app.core.metrics import router as metrics_router
from app.core.middleware import (
    AccessLogMiddleware,
    MetaDataMiddleware,
    MetricsMiddleware,
    SecurityHeadersMiddleware,
)
from app.core.responses import SuccessResponse, ok_response
from app.core.tasks import build_broker
from app.modules import MODULES

logger = get_logger(__name__)


def _unbind_session_factory(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Drop the engine binding so no code uses a disposed engine."""
    session_factory.configure(bind=None)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the app's resources on startup and close them on shutdown.

    Every resource registers its cleanup on an `AsyncExitStack` right after
    it is created, so a failure later in the startup closes everything that
    was already open, in reverse order, before the error propagates.
    """
    settings: Settings = app.state.settings
    session_factory: async_sessionmaker[AsyncSession] = app.state.session_factory

    setup_logging(settings)
    init_metrics(
        app_name=app.title,
        app_version=app.version,
        environment=settings.ENVIRONMENT.value,
    )
    # One consolidated structured startup event.
    logger.info(
        "app.startup",
        pid=os.getpid(),
        cwd=os.getcwd(),
        app_name=app.title,
        app_version=app.version,
        app_description=app.description,
        environment=settings.ENVIRONMENT.value.upper(),
        host=settings.HOST,
        port=settings.PORT,
    )

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

        # Redis client and cashews route cache.
        app.state.cache = await init_cache_pool(settings)
        stack.push_async_callback(close_cache_pool, app.state.cache)
        init_route_cache(settings)
        stack.push_async_callback(close_route_cache, settings)

        # Taskiq broker for enqueueing. Shared tasks (`app.core.task`) send
        # to the default broker; a later lifespan replaces it.
        broker = build_broker(settings)
        stack.push_async_callback(broker.shutdown)
        await broker.startup()
        async_shared_broker.default_broker(broker)
        app.state.broker = broker

        app.state.start_time = datetime.now()

        yield

        logger.info("app.shutdown")

    logger.info("app.shutdown_complete")


def _add_root_route(app: FastAPI, settings: Settings) -> None:
    """Register `GET /` (needs the settings for its cache TTL)."""

    @app.get("/", response_model=SuccessResponse)
    @cache(
        ttl=cache_control_ttl(default=settings.ROUTE_CACHE_TTL),
        key="route:root",
    )
    async def root(request: Request) -> JSONResponse:
        """Root endpoint with standardized response."""
        start_time = datetime.now()
        response_data = {
            "message": "Welcome to the FastAPI application with JWT authentication!",
            "docs_url": "/docs" if not settings.is_production else None,
            "metrics_url": "/metrics" if settings.METRICS_ENABLED else None,
            "auth_url": "/auth",
        }
        return ok_response(data=response_data, request=request, start_time=start_time)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Assemble the FastAPI application; performs no I/O.

    Args:
        settings: Defaults to `get_settings()`. The given settings drive the
            app assembly and the lifespan resources; request-time code reads
            `get_settings()` (`SettingsDep`).

    Returns:
        The app with every module router of `MODULES` mounted.
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
        # FastAPI >= 0.142 emits its own OpenTelemetry spans, metrics and log
        # records through the global providers. With OTLP_LOGS_ENABLED the
        # logging setup installs a global logger provider, and FastAPI would
        # then export unhandled exceptions with their message and stack trace
        # (which can carry personal data), bypassing log_unexpected_error.
        # Off until the observability redesign decides.
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

    # Exception handlers
    app.add_exception_handler(Exception, generic_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(AuthenticationError, authentication_exception_handler)

    # Middleware (order unchanged: the last added runs first)
    app.add_middleware(MetaDataMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],  # List your specific frontend URLs
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if settings.ROUTE_CACHE_ENABLED:
        app.add_middleware(CacheEtagMiddleware)
        app.add_middleware(CacheRequestControlMiddleware)
    # GZip compression for responses larger than 1KB
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(SecurityHeadersMiddleware)
    # Metrics middleware (before other middleware for accurate timing)
    if settings.METRICS_ENABLED:
        app.add_middleware(MetricsMiddleware)
    if settings.ACCESS_LOG_ENABLED:
        app.add_middleware(AccessLogMiddleware)

    # Routers
    if settings.METRICS_ENABLED:
        app.include_router(metrics_router, prefix="/metrics", tags=["monitoring"])
    for module in MODULES:
        app.include_router(module.router)
    _add_root_route(app, settings)

    return app


# ASGI entrypoint ([tool.fastapi] entrypoint, uvicorn "app.main:app").
app = create_app()
