"""The request middleware: request id, access log, metrics, headers, body limit.

One pure ASGI middleware. It runs in the request's own task, so the request
id it binds is in every log line and problem response of the request. It
also renders the 500 for an unhandled exception and the 413 for a body above
the limit, so those responses get the same headers, metrics and access log
as every other one. A dependency that needs the response a route sent (the
idempotency guard) registers a `ResponseRecorder` under `RESPONSE_RECORDER`
in the scope; the middleware hands it a copy of every response message.

`create_app` adds it inside Starlette's `CORSMiddleware`: the 500 rendered
here passes CORS on the way out and keeps the CORS headers. CORS preflights
are answered by `CORSMiddleware` itself and do not reach this middleware.
"""

import re
import time
import uuid
import weakref
from collections.abc import Iterable
from typing import Protocol

import structlog
from fastapi.routing import iter_route_contexts
from prometheus_client import Counter, Histogram
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import (
    BodyTooLargeError,
    body_too_large_response,
    internal_error_response,
    log_unexpected_error,
)
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

RESPONSE_RECORDER = "app.response_recorder"


class ResponseRecorder(Protocol):
    """Receives a copy of every response message of the request."""

    def record(self, message: Message) -> None:
        """Take a copy of `message` before the middleware adds its headers."""
        ...


# app -> {id(route): full path template}; built on the app's first lookup,
# when every route is registered. Keyed by id because Starlette routes
# define __eq__ and are unhashable; the app keeps them alive.
_route_templates: weakref.WeakKeyDictionary[object, dict[int, str]] = (
    weakref.WeakKeyDictionary()
)


def route_template(app: object, route: object | None) -> str:
    """Full path template of `route`, `scope["route"]` of a request in `app`.

    Unmatched requests (404 scans) and routes inside mounted sub-apps get
    `unmatched`, so client paths never become a label or a key.
    """
    if route is None:
        return UNMATCHED_ROUTE
    templates = _route_templates.get(app)
    if templates is None:
        routes = getattr(app, "routes", [])
        templates = {
            id(context.original_route): context.path_format
            for context in iter_route_contexts(routes)
            if context.path_format
        }
        _route_templates[app] = templates
    return templates.get(id(route), UNMATCHED_ROUTE)


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
    headers: Iterable[tuple[bytes, bytes]] = scope["headers"]
    for name, value in headers:
        if name == b"x-request-id":
            candidate = value.decode("latin-1")
            if _REQUEST_ID.fullmatch(candidate):
                return candidate
            break
    return str(uuid.uuid4())


def content_length_of(scope: Scope) -> int | None:
    """The declared `Content-Length`, or None if absent or not a number.

    The server refuses a malformed value before the app sees the request;
    without one (a chunked body) only the received bytes count.
    """
    headers: Iterable[tuple[bytes, bytes]] = scope["headers"]
    for name, value in headers:
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


def _stamp_headers(start: Message, request_id: str, *, no_store: bool) -> None:
    """Add the security headers, `X-Request-ID` and (API) `Cache-Control`."""
    headers = MutableHeaders(scope=start)
    for name, value in _SECURITY_HEADERS.items():
        headers[name] = value
    headers["X-Request-ID"] = request_id
    if no_store and "cache-control" not in headers:
        headers["Cache-Control"] = "no-store"


class RequestMiddleware:
    """Request id, access log, HTTP metrics, security headers and the body limit.

    Args:
        app: The wrapped ASGI app.
        api_prefix: Responses under this path prefix get
            `Cache-Control: no-store` unless the route set `Cache-Control`.
        access_log: Emit one `http.access` event per request.
        metrics: Record `http_requests_total` and
            `http_request_duration_seconds` (never for `/metrics` itself).
        max_body_bytes: Largest request body accepted. A larger declared
            `Content-Length` is answered with 413 before the app runs; a body
            without one (chunked) is cut off with 413 once the bytes read
            exceed the limit.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        api_prefix: str,
        access_log: bool,
        metrics: bool,
        max_body_bytes: int,
    ) -> None:
        self.app = app
        self.api_prefix = api_prefix
        self.access_log = access_log
        self.metrics = metrics
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Handle one ASGI connection; only HTTP requests are instrumented."""
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
            # Read per message: a dependency registers it while the request runs.
            recorder: ResponseRecorder | None = scope.get(RESPONSE_RECORDER)
            if recorder is not None:
                recorder.record(message)
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                _stamp_headers(message, request_id, no_store=no_store)
            await send(message)

        received = 0

        async def receive_with_limit() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise BodyTooLargeError
            return message

        try:
            declared = content_length_of(scope)
            if declared is not None and declared > self.max_body_bytes:
                await body_too_large_response()(scope, receive, send_with_headers)
            else:
                await self.app(scope, receive_with_limit, send_with_headers)
        except BodyTooLargeError:
            # Only a body read outside FastAPI's routing (no exception
            # handler on the way) gets here; a started response cannot change.
            if not response_started:
                await body_too_large_response()(scope, receive, send_with_headers)
        except Exception as exc:  # noqa: BLE001 - this is the app's last error boundary
            # Not re-raised: the server would log it a second time, with the
            # database error text that log_unexpected_error leaves out.
            log_unexpected_error(logger, "app.unhandled_exception", exc)
            if not response_started:
                await internal_error_response()(scope, receive, send_with_headers)
        finally:
            duration = time.perf_counter() - start
            route = route_template(app, scope.get("route"))
            self._record(scope, route, status, duration)
            structlog.contextvars.clear_contextvars()

    def _under_api_prefix(self, path: str) -> bool:
        return path == self.api_prefix or path.startswith(f"{self.api_prefix}/")

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
