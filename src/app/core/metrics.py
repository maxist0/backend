"""Prometheus metrics: the app registry, helpers and the `/metrics` router.

`create_app()` mounts `router` under `/metrics` when `METRICS_ENABLED`.
"""

import os
import time
from datetime import datetime

from fastapi import APIRouter, Request, Response
from prometheus_client import (
    Counter,
    Histogram,
    Gauge,
    Info,
    generate_latest,
    CONTENT_TYPE_LATEST,
)
from prometheus_client.core import CollectorRegistry

from app.core.config import SettingsDep
from app.core.responses import SuccessResponse, ok_response


# Create a custom registry for this application
REGISTRY = CollectorRegistry()

# HTTP Request metrics
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total number of HTTP requests",
    ["method", "endpoint", "status_code"],
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "endpoint"],
    registry=REGISTRY,
)

HTTP_REQUEST_SIZE_BYTES = Histogram(
    "http_request_size_bytes",
    "HTTP request size in bytes",
    ["method", "endpoint"],
    registry=REGISTRY,
)

HTTP_RESPONSE_SIZE_BYTES = Histogram(
    "http_response_size_bytes",
    "HTTP response size in bytes",
    ["method", "endpoint"],
    registry=REGISTRY,
)

# Application metrics
ACTIVE_CONNECTIONS = Gauge(
    "active_connections", "Number of active connections", registry=REGISTRY
)

APPLICATION_INFO = Info(
    "application_info", "Application information", registry=REGISTRY
)

# Business logic metrics
API_ERRORS_TOTAL = Counter(
    "api_errors_total",
    "Total number of API errors",
    ["error_type", "endpoint"],
    registry=REGISTRY,
)

DATABASE_OPERATIONS_TOTAL = Counter(
    "database_operations_total",
    "Total number of database operations",
    ["operation", "table"],
    registry=REGISTRY,
)

DATABASE_OPERATION_DURATION_SECONDS = Histogram(
    "database_operation_duration_seconds",
    "Database operation duration in seconds",
    ["operation", "table"],
    registry=REGISTRY,
)


def init_metrics(app_name: str, app_version: str, environment: str):
    """
    Initialize application metrics.
    """
    APPLICATION_INFO.info(
        {"name": app_name, "version": app_version, "environment": environment}
    )


def get_metrics() -> bytes:
    """
    Get metrics in Prometheus format.
    """
    return generate_latest(REGISTRY)


def get_metrics_content_type() -> str:
    """
    Get the content type for metrics.
    """
    return str(CONTENT_TYPE_LATEST)


# Helper functions for custom metrics


def record_database_operation(
    operation: str, table: str, duration: float | None = None
):
    """
    Record a database operation.
    """
    DATABASE_OPERATIONS_TOTAL.labels(operation=operation, table=table).inc()
    if duration is not None:
        DATABASE_OPERATION_DURATION_SECONDS.labels(
            operation=operation, table=table
        ).observe(duration)


def record_api_error(error_type: str, endpoint: str):
    """
    Record an API error.
    """
    API_ERRORS_TOTAL.labels(error_type=error_type, endpoint=endpoint).inc()


# Context managers for timing operations


class DatabaseTimer:
    """
    Context manager for timing database operations.
    """

    def __init__(self, operation: str, table: str):
        self.operation = operation
        self.table = table
        self.start_time = None

    def __enter__(self):
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        duration = time.perf_counter() - self.start_time
        record_database_operation(self.operation, self.table, duration)

        if exc_type is not None:
            record_api_error(exc_type.__name__, f"database.{self.table}")


router = APIRouter()


@router.get("/")
async def metrics(request: Request):
    """
    Prometheus metrics endpoint.

    Returns metrics in Prometheus format for scraping by monitoring systems.
    """
    metrics_data = get_metrics()
    return Response(content=metrics_data, media_type=get_metrics_content_type())


@router.get("/health", response_model=SuccessResponse)
async def health_check(request: Request, settings: SettingsDep):
    """
    Health check endpoint with standardized response.

    Simple endpoint to verify the API is running.
    """
    start_time = (
        request.app.state.start_time
        if hasattr(request.app.state, "start_time")
        else datetime.now()
    )

    health_data = {
        "status": "healthy",
        "service": settings.APP_NAME,
        "metrics_available": True,
        "pod_name": os.getenv("MY_POD_NAME"),
        "node_name": os.getenv("MY_NODE_NAME"),
        "uptime_seconds": datetime.now().timestamp() - start_time.timestamp()
        if start_time
        else None,
    }

    return ok_response(data=health_data, request=request, start_time=start_time)
