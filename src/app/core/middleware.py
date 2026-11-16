"""HTTP middleware: request id, access log, metrics and security headers."""

import re
import time
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from fastapi.routing import iter_route_contexts
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_access_logger

# (path regex, full path template) of every place a route object is reachable.
_RouteTemplates = tuple[tuple[re.Pattern[str], str], ...]

# The request method is client-controlled; as a metric label it must come from
# a fixed set, or invented methods create unbounded time series.
_METRIC_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)


def _metric_method(method: str) -> str:
    """Map the request method onto the bounded label set (else `other`)."""
    return method if method in _METRIC_METHODS else "other"


def _get_client_ip(request: Request) -> str:
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
    - HTTP method and path (without query string: it can carry tokens)
    - User agent
    - Response status code and size
    - Processing duration (ms)

    The Referer header is not logged either (it can carry tokens and PII).
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        access_logger = get_access_logger()

        # Capture request metadata before processing
        client_ip = _get_client_ip(request)
        method = request.method
        path = request.url.path
        user_agent = request.headers.get("user-agent", "-")

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
            path=path,
            status_code=status_code,
            content_length=content_length,
            duration_ms=duration_ms,
            user_agent=user_agent,
        )
        return response


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    Middleware to collect HTTP metrics automatically.
    """

    def __init__(self, app, registry=None):
        super().__init__(app)
        from app.core.metrics import REGISTRY

        self.registry = registry or REGISTRY
        # id(route) -> (route, templates). The route object is kept so its id
        # cannot be reused; routes live as long as the app.
        self._templates: dict[int, tuple[Any, _RouteTemplates]] = {}

    async def dispatch(self, request: Request, call_next):
        # Skip metrics collection for the metrics endpoint itself
        if request.url.path == "/metrics":
            return await call_next(request)

        from app.core.metrics import (
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

        method = _metric_method(request.method)
        try:
            # Process the request; routing sets scope["route"] on a match.
            response: Response = await call_next(request)
            path_template = self._get_path_template(request)

            # Measure request size
            request_size = int(request.headers.get("content-length", 0))
            HTTP_REQUEST_SIZE_BYTES.labels(
                method=method, endpoint=path_template
            ).observe(request_size)

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
                except AttributeError, TypeError:
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
        Return the route template (e.g. `/users/{id}`) of the matched route.

        Read after `call_next`: the router stores the matched route in
        `scope["route"]`. Unmatched requests (404 scans) all share the label
        `unmatched`, so arbitrary paths cannot create new label values
        (bounded cardinality).
        """
        route = request.scope.get("route")
        if route is None:
            return "unmatched"
        templates = self._route_templates(request, route)
        if len(templates) == 1:
            return templates[0][1]
        if templates:
            # The same router included under several prefixes (e.g. two API
            # versions): pick the template that matched this request.
            route_path = _route_path(request.scope)
            for regex, template in templates:
                if regex.match(route_path):
                    return template
        path = getattr(route, "path", None)
        return path if isinstance(path, str) and path else "unmatched"

    def _route_templates(self, request: Request, route: Any) -> _RouteTemplates:
        """Full path templates of *route* in this app (cached per route).

        Since FastAPI 0.137 included routers are kept as a tree: the matched
        `scope["route"]` is the router's own `APIRoute` whose `path` lacks
        the include prefixes (`/login` instead of `/auth/login`). The
        prefixed template comes from `fastapi.routing.iter_route_contexts`.
        Routes outside the app's tree (e.g. inside the mounted admin app)
        yield no template and keep their own `path`.
        """
        cached = self._templates.get(id(route))
        if cached is not None and cached[0] is route:
            return cached[1]
        templates: _RouteTemplates = tuple(
            (context.path_regex, context.path_format)
            for context in iter_route_contexts(request.app.routes)
            if context.original_route is route
            and isinstance(context.path_format, str)
            and context.path_format
        )
        self._templates[id(route)] = (route, templates)
        return templates


def _route_path(scope: Any) -> str:
    """The request path relative to `root_path` (what routes match against)."""
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        rest = path[len(root_path) :]
        if not rest or rest.startswith("/"):
            return rest
    return path


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
        # Drop context left over from a previous request on this task
        # (hot reload, task reuse) before binding this request's id.
        structlog.contextvars.clear_contextvars()

        # Use caller-supplied request id when present, otherwise generate one.
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
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
