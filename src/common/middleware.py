# src/common/middleware.py
import time
import uuid
from typing import Callable

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.common.logging import get_access_logger


def _get_client_ip(self, request: Request) -> str:
        """
        Get the client IP address, considering proxy headers.

        Checks in order:
        1. X-Forwarded-For (first IP in the chain)
        2. X-Real-IP
        3. Direct client host
        """
        # Check X-Forwarded-For header (may contain multiple IPs)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # Take the first IP (original client)
            return forwarded_for.split(",")[0].strip()

        # Check X-Real-IP header
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip

        # Fall back to direct client
        if request.client:
            return request.client.host

        return "-"


class AccessLogMiddleware(BaseHTTPMiddleware):
    """
    Middleware to log HTTP access information as structured JSON events:
    - Client IP address
    - HTTP method and path
    - User agent
    - Response status code and size
    - Processing duration (ms)
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        access_logger = get_access_logger()

        # Capture request metadata before processing
        client_ip = _get_client_ip(request)
        method = request.method
        path = request.url.path
        query_string = request.url.query
        full_path = f"{path}?{query_string}" if query_string else path
        user_agent = request.headers.get("user-agent", "-")
        referer = request.headers.get("referer", "-")

        # Process the request and measure duration
        start = __import__("time").perf_counter()
        response: Response = await call_next(request)
        duration_ms = round((__import__("time").perf_counter() - start) * 1000, 2)

        # Get response info
        status_code = response.status_code
        content_length = response.headers.get("content-length", "-")

        # Emit a single structured access event
        access_logger.info(
            "http.access",
            client_ip=client_ip,
            method=method,
            path=full_path,
            status_code=status_code,
            content_length=content_length,
            duration_ms=duration_ms,
            user_agent=user_agent,
            referer=referer,
        )
        return response

    


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware to collect HTTP metrics automatically.
    """

    def __init__(self, app, registry=None):
        super().__init__(app)
        from src.common.metrics import REGISTRY

        self.registry = registry or REGISTRY

    async def dispatch(self, request: Request, call_next):
        # Skip metrics collection for the metrics endpoint itself
        if request.url.path == "/metrics":
            return await call_next(request)

        from src.common.metrics import (
            HTTP_REQUESTS_TOTAL,
            HTTP_REQUEST_DURATION_SECONDS,
            HTTP_REQUEST_SIZE_BYTES,
            HTTP_RESPONSE_SIZE_BYTES,
            ACTIVE_CONNECTIONS,
            API_ERRORS_TOTAL,
        )

        # Record start time
        start_time = time.perf_counter()

        # Increment active connections
        ACTIVE_CONNECTIONS.inc()

        try:
            # Get request information
            method = request.method
            path_template = self._get_path_template(request)

            # Measure request size
            request_size = int(request.headers.get("content-length", 0))
            HTTP_REQUEST_SIZE_BYTES.labels(
                method=method, endpoint=path_template
            ).observe(request_size)

            # Process the request
            response: Response = await call_next(request)

            # Calculate processing time
            duration = time.perf_counter() - start_time

            # Record metrics
            status_code = str(response.status_code)
            HTTP_REQUESTS_TOTAL.labels(
                method=method, endpoint=path_template, status_code=status_code
            ).inc()
            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=method, endpoint=path_template
            ).observe(duration)

            # Measure response size
            response_size = int(response.headers.get("content-length", 0))
            if response_size == 0 and hasattr(response, "body"):
                # Try to get size from body if content-length not set
                try:
                    response_size = len(response.body)
                except (AttributeError, TypeError):
                    response_size = 0

            HTTP_RESPONSE_SIZE_BYTES.labels(
                method=method, endpoint=path_template
            ).observe(response_size)

            return response

        except Exception as e:
            # Record error metrics
            error_type = type(e).__name__
            path_template = self._get_path_template(request)
            API_ERRORS_TOTAL.labels(error_type=error_type, endpoint=path_template).inc()
            raise
        finally:
            # Decrement active connections
            ACTIVE_CONNECTIONS.dec()

    def _get_path_template(self, request: Request) -> str:
        """
        Get the path template for the request, falling back to the raw path.
        This helps group similar paths together (e.g., /users/{id}).
        """
        try:
            # Try to get the route pattern if available
            if hasattr(request, "route") and hasattr(request.route, "path"):
                return request.route.path
        except AttributeError:
            pass

        # Fallback to raw path
        path = request.url.path

        # Simple path normalization for common patterns
        # Convert paths like /users/123 to /users/{id}
        import re

        path = re.sub(r"/\d+", "/{id}", path)
        path = re.sub(r"/[a-f0-9-]{36}", "/{uuid}", path)  # UUID pattern

        return path

class CORSMiddleware(BaseHTTPMiddleware):

    """
    Middleware to handle Cross-Origin Resource Sharing (CORS).
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Set CORS headers
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Credentials"] = "true"

        return response
    

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add security-related HTTP headers.
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Set security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "geolocation=()"

        return response
    

class MetaDataMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add standardized metadata to all responses.
    """

    async def dispatch(self, request: Request, call_next):

        # ------------------------------------------------------------------
        # Per-request structlog context
        # Clear any leftover context from a previous request (hot-reload /
        # task-reuse), then bind a fresh request_id for this request.
        # ------------------------------------------------------------------
        structlog.contextvars.clear_contextvars()

        # Use caller-supplied request id when present, otherwise generate one.
        request_id = (
            request.headers.get("x-request-id") or str(uuid.uuid4())
        )
        structlog.contextvars.bind_contextvars(request_id=request_id)

        # Record start time
        start_time = time.perf_counter()

        # Process the request
        response: Response = await call_next(request)

        # Calculate processing time
        process_time = time.perf_counter() - start_time
        process_time_ms = round(process_time * 1000, 2)

         # Add processing time header to response
        response.headers["X-Process-Time"] = f"{process_time_ms}ms"

        # Forward the request id so clients / gateways can correlate logs.
        response.headers["X-Request-ID"] = request_id

         
        # Clean up context so it doesn't bleed into the next request.
        structlog.contextvars.clear_contextvars()
        return response




