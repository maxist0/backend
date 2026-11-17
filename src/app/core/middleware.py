"""The request middleware: request id, access log, HTTP metrics, security headers.

One pure ASGI middleware. It runs in the request's own task, so the request
id it binds is in every log line and problem response of the request. It
also renders the 500 for an unhandled exception, so that response gets the
same headers, metrics and access log as every other one.

`create_app` adds it inside Starlette's `CORSMiddleware`: the 500 rendered
here passes CORS on the way out and keeps the CORS headers. CORS preflights
are answered by `CORSMiddleware` itself and do not reach this middleware.
"""

import re
import time
import uuid

import structlog
from fastapi.routing import iter_route_contexts
from prometheus_client import Counter, Histogram
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import internal_error_response, log_unexpected_error
from app.core.health import METRICS_PATH
from app.core.logging import get_logger

logger = get_logger(__name__)

HTTP_REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests by method, route template and status.",
    ["method", "route", "status"],
)
# prometheus_client's default buckets span 5 ms to 10 s.
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds by method and route template.",
    ["method", "route"],
)

UNMATCHED_ROUTE = "unmatched"

# The method is client-controlled; as a metric label it must come from a fixed
# set, or invented methods create unbounded time series.
_METRIC_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
)

# A client-supplied id is echoed into headers and logs, so only a short token
# of safe characters is accepted.
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

# No HSTS: the TLS-terminating proxy owns it.
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=()",
}


def metric_method(method: str) -> str:
    """Map the request method onto the bounded label set (else `other`)."""
    return method if method in _METRIC_METHODS else "other"


def request_id_from(scope: Scope) -> str:
    """Return the client's `X-Request-ID` if it is a safe token, else a new UUID."""
    for name, value in scope["headers"]:
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            if _REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return str(uuid.uuid4())


class RequestMiddleware:
    """Request id, access log, HTTP metrics and security headers for HTTP requests.

    Args:
        app: The wrapped ASGI app.
        api_prefix: Responses under this path prefix get
            `Cache-Control: no-store` unless the route set `Cache-Control`.
        access_log: Emit one `http.access` event per request.
        metrics: Record `http_requests_total` and
            `http_request_duration_seconds` (never for `/metrics` itself).
    """

    def __init__(
        self, app: ASGIApp, *, api_prefix: str, access_log: bool, metrics: bool
    ) -> None:
        self.app = app
        self.api_prefix = api_prefix
        self.access_log = access_log
        self.metrics = metrics
        # id(route) -> full path template; built on the first request, when
        # every route is registered. Keyed by id because Starlette routes
        # define __eq__ and are unhashable; the app keeps them alive.
        self._templates: dict[int, str] | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Read before the call: a mounted sub-app replaces scope["app"].
        app = scope.get("app")
        request_id = request_id_from(scope)
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        no_store = self._under_api_prefix(scope["path"])
        status = 500
        response_started = False
        start = time.perf_counter()

        async def send_with_headers(message: Message) -> None:
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers[name] = value
                headers["X-Request-ID"] = request_id
                if no_store and "cache-control" not in headers:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as exc:
            # Not re-raised: the server would log it a second time, with the
            # database error text that log_unexpected_error leaves out.
            log_unexpected_error(logger, "app.unhandled_exception", exc)
            if not response_started:
                await internal_error_response()(scope, receive, send_with_headers)
        finally:
            duration = time.perf_counter() - start
            route = self._route_template(app, scope)
            self._record(scope, route, status, duration)
            structlog.contextvars.clear_contextvars()

    def _under_api_prefix(self, path: str) -> bool:
        return path == self.api_prefix or path.startswith(f"{self.api_prefix}/")

    def _route_template(self, app: object, scope: Scope) -> str:
        """Full path template of the matched route, or `unmatched`.

        Unmatched requests (404 scans) and routes inside mounted sub-apps
        share one label, so client paths cannot create new label values.
        """
        route = scope.get("route")
        if route is None:
            return UNMATCHED_ROUTE
        if self._templates is None:
            routes = getattr(app, "routes", [])
            self._templates = {
                id(context.original_route): context.path_format
                for context in iter_route_contexts(routes)
                if context.path_format
            }
        return self._templates.get(id(route), UNMATCHED_ROUTE)

    def _record(self, scope: Scope, route: str, status: int, duration: float) -> None:
        if self.metrics and route != METRICS_PATH:
            method = metric_method(scope["method"])
            HTTP_REQUESTS.labels(method=method, route=route, status=str(status)).inc()
            HTTP_REQUEST_DURATION.labels(method=method, route=route).observe(duration)
        if self.access_log:
            client = scope.get("client")
            logger.info(
                "http.access",
                method=scope["method"],
                path=scope["path"],
                route=route,
                status=status,
                duration_ms=round(duration * 1000, 2),
                client_ip=client[0] if client else None,
            )
