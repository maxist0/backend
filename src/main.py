#src/main.py

from contextlib import asynccontextmanager
import os
from fastapi import FastAPI
from src.common.logging import logger
from src.common.config import settings
from src.common.middleware import AccessLogMiddleware, MetricsMiddleware
from src.common.metrics import init_metrics
from src.api.metrics.router import router as metrics_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup

    # Initialize metrics
    init_metrics(
        app_name=app.title,
        app_version=app.version,
        environment=settings.ENVIRONMENT.value
    )

    # Log Start, PID,Port,IP etc.
    logger.info("Application starting up")
    logger.info(f"Process ID: {os.getpid()}")
    logger.info(f"Working Directory: {os.getcwd()}")
    logger.info(f"App Name: {app.title}")
    logger.info(f"App Version: {app.version}")
    logger.info(f"App Description: {app.description}")
    logger.info(f"Environment: {settings.ENVIRONMENT.value.upper()}")
    logger.info(f"Address: {settings.HOST}")
    logger.info(f"Port: {settings.PORT}")
    logger.info(f"Reload Enabled: {settings.RELOAD}")


    yield
    # Shutdown
    logger.info("Application shutting down")


app = FastAPI(
    title=settings.APP_NAME,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    # Disable Swagger UI and ReDoc in production
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

# Add metrics middleware (before other middleware for accurate timing)
if settings.METRICS_ENABLED:
    app.add_middleware(MetricsMiddleware)

# Add access logging middleware
if settings.ACCESS_LOG_ENABLED:
    app.add_middleware(AccessLogMiddleware)

# Include API routes
if settings.METRICS_ENABLED:
    app.include_router(metrics_router,prefix="/metrics", tags=["monitoring"])


@app.get("/")
async def root():
    """Root endpoint."""
    return {
        "message": "Welcome to the FastAPI application with metrics!",
        "docs_url": "/docs" if not settings.is_production else None,
        "metrics_url": "/metrics" if settings.METRICS_ENABLED else None,
        "health_url": "/health"
    }