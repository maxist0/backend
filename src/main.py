# src/main.py

"""
Main application entry point for FastAPI app with metrics and logging.
Sets up the FastAPI app, middleware, routes, and lifecycle events.
"""

# Imports
from contextlib import asynccontextmanager
from datetime import datetime
import os
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from src.api.auth.exceptions import AuthenticationError
from src.common.logging import get_logger
from src.common.config import settings
from src.common.database import init_db, close_db
from src.common.cache import init_cache_pool, init_fastapi_cache
from src.common.middleware import (
    AccessLogMiddleware,
    MetricsMiddleware,
    SecurityHeadersMiddleware,
    MetaDataMiddleware,
)
from fastapi.middleware.cors import CORSMiddleware
from src.common.metrics import init_metrics
from src.common.responses import ok_response, SuccessResponse
from src.api.metrics.router import router as metrics_router
from src.api.auth.router import router as auth_router
from src.api.user.router import router as user_router

from src.common.exceptions import (
    generic_exception_handler,
    validation_exception_handler,
    authentication_exception_handler,
)

from fastapi.middleware.gzip import GZipMiddleware

# Logger for this module
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application startup and shutdown events.

    Set up resources like database connections, caches, and logging.

    """

    # Startup

    # Initialize metrics
    init_metrics(
        app_name=app.title,
        app_version=app.version,
        environment=settings.ENVIRONMENT.value,
    )

    # Log Start, PID, Port, IP etc. – one consolidated structured event.
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

    ## Initialize DB connection pool
    await init_db()

    ## Initialize Redis and fastapi-cache2
    app.state.cache = await init_cache_pool()
    init_fastapi_cache(app.state.cache)

    app.state.start_time = datetime.now()

    yield

    # Shutdown
    logger.info("app.shutdown")

    ## Close DB connection pool and cache
    await close_db()
    if app.state.cache:
        await app.state.cache.close()
        logger.info("app.cache_closed")

    logger.info("app.shutdown_complete")


## FastAPI Application Instance
"""
Create FastAPI application instance with metadata and lifecycle management.
"""

assert settings is not None, (
    "Settings must be initialized before creating the app instance"
)

app = FastAPI(
    title=settings.APP_NAME,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

## Exception Handlers
app.add_exception_handler(Exception, generic_exception_handler)
app.add_exception_handler(RequestValidationError, validation_exception_handler)
app.add_exception_handler(AuthenticationError, authentication_exception_handler)

## Middleware Configuration
"""
Configure middleware for metrics and access logging.
"""

app.add_middleware(MetaDataMiddleware)  # Add metadata middleware to all requests
# app.add_middleware(CORSMiddleware)  # Add Cors middleware with configured settings
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],  # List your specific frontend URLs
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    GZipMiddleware, minimum_size=1000
)  # Add GZip compression for responses larger than 1KB
app.add_middleware(SecurityHeadersMiddleware)
# Add metrics middleware (before other middleware for accurate timing)
if settings.METRICS_ENABLED:
    app.add_middleware(MetricsMiddleware)
# Add access logging middleware
if settings.ACCESS_LOG_ENABLED:
    app.add_middleware(AccessLogMiddleware)


# Include API routes
"""
Include API routes for metrics and authentication.
"""
if settings.METRICS_ENABLED:
    app.include_router(metrics_router, prefix="/metrics", tags=["monitoring"])

# Include authentication routes
app.include_router(auth_router, prefix="/auth", tags=["authentication"])

# Include user profile routes
app.include_router(user_router, prefix="/user", tags=["user"])


# Root Endpoint
"""
Define the root endpoint with standardized response.
"""


@app.get("/", response_model=SuccessResponse)
async def root(request: Request):
    """Root endpoint with standardized response."""
    start_time = datetime.now()
    response_data = {
        "message": "Welcome to the FastAPI application with JWT authentication!",
        "docs_url": "/docs" if not settings.is_production else None,
        "metrics_url": "/metrics" if settings.METRICS_ENABLED else None,
        "auth_url": "/auth",
    }
    return ok_response(data=response_data, request=request, start_time=start_time)
