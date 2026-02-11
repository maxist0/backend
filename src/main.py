#src/main.py

"""
Main application entry point for FastAPI app with metrics and logging.
Sets up the FastAPI app, middleware, routes, and lifecycle events.
"""

# Imports
from contextlib import asynccontextmanager
from datetime import datetime
import os
import time
from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse
from src.common.cache import init_cache_pool
from src.common.logging import get_logger
from src.common.config import settings,init_settings
from src.common.middleware import AccessLogMiddleware, MetricsMiddleware
from src.common.metrics import init_metrics
from src.common.responses import ok_response, SuccessResponse
from src.api.metrics.router import router as metrics_router
from src.api.auth.router import router as auth_router

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


    ## Initialize DB connection pools and chache
    app.state.db = None  # Placeholder for DB connection pool
    app.state.cache = None  # Placeholder for cache connection
    
    yield
    
    # Shutdown
    logger.info("Application shutting down ...")

    ## Close DB connection pools and cache
    if app.state.db:
        await app.state.db.close()
        logger.info("Database connection pool closed.")
    if app.state.cache:
        await app.state.cache.close()
        logger.info("Cache connection pool closed.")
    
    logger.info("Application shutdown complete.")


## FastAPI Application Instance
"""
Create FastAPI application instance with metadata and lifecycle management.
"""

# Check if settings is initialized, if not initialize with default environment
if settings is None:
    print("Settings not initialized, initializing with default environment 'dev'")
    settings = init_settings("dev")



app = FastAPI(
    title=settings.APP_NAME,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)


## Middleware Configuration
"""
Configure middleware for metrics and access logging.
"""

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
    app.include_router(metrics_router,prefix="/metrics", tags=["monitoring"])

# Include authentication routes
app.include_router(auth_router, prefix="/auth", tags=["authentication"])



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
        "auth_url": "/auth"
    }
    return ok_response(data=response_data, request=request, start_time=start_time)

