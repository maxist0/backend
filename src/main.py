#src/main.py

from contextlib import asynccontextmanager
import os
from fastapi import FastAPI
from src.common.logging import logger
from src.common.config import settings
from src.common.middleware import AccessLogMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup

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

# Add access logging middleware
if settings.ACCESS_LOG_ENABLED:
    app.add_middleware(AccessLogMiddleware)