"""The request body limit of the request middleware (413).

A declared `Content-Length` above the limit is refused before the app runs;
a body without one (chunked) is cut off once the bytes read pass the limit.
On the real app both answers are RFC 9457 problems with the framework code
`content_too_large`.
"""

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route
from starlette.types import Message, Receive, Scope, Send

from app.core.middleware import RequestMiddleware
from app.main import create_app
from tests.helpers import SettingsFactory, log_events, new_test_client, run_async

LIMIT = 64


def _chunked(body: bytes) -> Iterator[bytes]:
    """A generator body: httpx sends it chunked, without `Content-Length`."""
    yield body


class _Echo:
    """A Starlette endpoint that reads the whole body and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    async def endpoint(self, request: Request) -> Response:
        self.calls += 1
        body = await request.body()
        return PlainTextResponse(str(len(body)))


def _client(echo: _Echo) -> TestClient:
    app = Starlette(routes=[Route("/api/v1/echo", echo.endpoint, methods=["POST"])])
    app.add_middleware(
        RequestMiddleware,
        api_prefix="/api/v1",
        access_log=True,
        metrics=True,
        max_body_bytes=LIMIT,
    )
    return new_test_client(app)


def _requests(route: str, status: str) -> float:
    labels = {"method": "POST", "route": route, "status": status}
    return REGISTRY.get_sample_value("http_requests_total", labels) or 0.0


def _drive(
    middleware: RequestMiddleware,
    chunks: list[bytes],
    headers: list[tuple[bytes, bytes]],
) -> list[Message]:
    """Run one request through `middleware`, the body split into `chunks`."""
    incoming: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]
    sent: list[Message] = []

    async def receive() -> Message:
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/raw",
        "headers": headers,
        "client": ("192.0.2.1", 1234),
    }
    run_async(middleware(scope, receive, send))
    return sent


async def _read_all(_scope: Scope, receive: Receive, send: Send) -> None:
    """A bare ASGI app without exception handling that reads the whole body."""
    while (await receive()).get("more_body", False):
        pass
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"read"})


class TestDeclaredLength:
    def test_a_larger_content_length_is_refused_before_the_app_runs(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)
        echo = _Echo()
        before = _requests("unmatched", "413")

        r = _client(echo).post("/api/v1/echo", content=b"x" * (LIMIT + 1))

        assert r.status_code == 413
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["code"] == "content_too_large"
        assert r.headers["x-request-id"]
        assert r.headers["x-frame-options"] == "DENY"
        assert echo.calls == 0
        assert _requests("unmatched", "413") == before + 1
        [event] = log_events(caplog, "http.access")
        assert event["status"] == 413

    def test_a_body_of_exactly_the_limit_passes(self) -> None:
        echo = _Echo()

        r = _client(echo).post("/api/v1/echo", content=b"x" * LIMIT)

        assert r.status_code == 200
        assert r.text == str(LIMIT)


class TestStreamedBody:
    def test_a_chunked_body_above_the_limit_is_cut_off(self) -> None:
        echo = _Echo()

        r = _client(echo).post("/api/v1/echo", content=_chunked(b"x" * (LIMIT + 1)))

        assert r.status_code == 413
        assert echo.calls == 1

    def test_a_chunked_body_within_the_limit_passes(self) -> None:
        r = _client(_Echo()).post("/api/v1/echo", content=_chunked(b"x" * LIMIT))

        assert r.status_code == 200
        assert r.text == str(LIMIT)

    def test_bytes_are_counted_across_chunks(self) -> None:
        middleware = RequestMiddleware(
            _read_all,
            api_prefix="/api/v1",
            access_log=False,
            metrics=False,
            max_body_bytes=LIMIT,
        )

        sent = _drive(middleware, [b"x" * 40, b"x" * 40], headers=[])

        start = sent[0]
        assert start["type"] == "http.response.start"
        assert start["status"] == 413

    def test_chunks_that_add_up_to_the_limit_pass(self) -> None:
        middleware = RequestMiddleware(
            _read_all,
            api_prefix="/api/v1",
            access_log=False,
            metrics=False,
            max_body_bytes=LIMIT,
        )

        sent = _drive(middleware, [b"x" * 32, b"x" * 32], headers=[])

        assert sent[0]["status"] == 200

    def test_a_started_response_is_left_alone(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The limit cannot replace a response that is already on the wire."""

        async def respond_then_read(
            _scope: Scope, receive: Receive, send: Send
        ) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            while (await receive()).get("more_body", False):
                pass

        middleware = RequestMiddleware(
            respond_then_read,
            api_prefix="/api/v1",
            access_log=False,
            metrics=False,
            max_body_bytes=LIMIT,
        )

        sent = _drive(middleware, [b"x" * 40, b"x" * 40], headers=[])

        assert [m["status"] for m in sent if m["type"] == "http.response.start"] == [
            200
        ]
        assert log_events(caplog, "app.unhandled_exception") == []


class TestOnTheApp:
    """The real app, without lifespan: a refused body never reaches a route."""

    @pytest.fixture
    def client(self, settings_factory: SettingsFactory) -> TestClient:
        return new_test_client(
            create_app(settings_factory(MAX_REQUEST_BODY_BYTES=1024))
        )

    def test_content_length_above_the_setting_is_a_413_problem(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/api/v1/auth/register",
            content=b"{" + b" " * 2048 + b"}",
            headers={"Content-Type": "application/json", "X-Request-ID": "big-1"},
        )

        assert r.status_code == 413
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json() == {
            "type": "about:blank",
            "title": "Content Too Large",
            "status": 413,
            "detail": "The request body is larger than the server accepts.",
            "code": "content_too_large",
            "request_id": "big-1",
        }

    def test_a_chunked_body_above_the_setting_is_a_413_problem(
        self, client: TestClient
    ) -> None:
        r = client.post(
            "/api/v1/auth/register",
            content=_chunked(b"{" + b" " * 2048 + b"}"),
            headers={"Content-Type": "application/json", "X-Request-ID": "big-2"},
        )

        assert r.status_code == 413
        assert r.headers["content-type"] == "application/problem+json"
        body = r.json()
        assert body["code"] == "content_too_large"
        assert body["request_id"] == "big-2"

    def test_a_large_login_form_is_a_413_problem(self, client: TestClient) -> None:
        """Form bodies count too: the login form is parsed after the check."""
        r = client.post(
            "/api/v1/auth/login",
            data={"username": "alice", "password": "x" * 2048},
        )

        assert r.status_code == 413
        assert r.json()["code"] == "content_too_large"

    def test_the_setting_is_bounded(self, settings_factory: SettingsFactory) -> None:
        for value in (1023, 104_857_601):
            with pytest.raises(ValueError, match="MAX_REQUEST_BODY_BYTES"):
                settings_factory(MAX_REQUEST_BODY_BYTES=value)
