"""RFC 9457 problem details: `AppError` rendering, the 422 field mapping and
every handler path through the real middleware stack (no Docker: the probe
routes touch neither the database nor Redis)."""

import pytest
import structlog
from fastapi import APIRouter, Header, Query
from pydantic import BaseModel

import app.main as main
from app.core import AppError, BadRequest, Conflict, Forbidden, NotFound, Unauthorized
from app.core.errors import (
    PROBLEM_JSON,
    field_errors,
    problem_response,
    problem_responses,
)
from tests.helpers import new_test_client


class _ProbeConflict(Conflict):
    code = "probe_conflict"
    detail = "The probe conflicts."


class _ProbeItem(BaseModel):
    name: str
    size: int


class _ProbeBody(BaseModel):
    items: list[_ProbeItem]


class _LeakyError(Exception):
    pass


_SECRET = "SECRET-db-row-alice@example.com"


def _probe_router() -> APIRouter:
    router = APIRouter(prefix="/probe")

    @router.post("/conflict")
    async def conflict() -> None:
        raise _ProbeConflict(headers={"Retry-After": "7"}, retry_after=7)

    @router.post("/items")
    async def items(
        body: _ProbeBody,
        limit: int = Query(le=100),
        x_probe: int = Header(),
    ) -> None:
        return None

    @router.get("/crash")
    async def crash() -> None:
        raise _LeakyError(_SECRET)

    return router


@pytest.fixture
def probe_client(settings_factory):
    app = main.create_app(settings_factory(ROUTE_CACHE_ENABLED=False))
    app.include_router(_probe_router())
    return new_test_client(app, raise_server_exceptions=False)


def _assert_problem(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["status"] == status
    assert body["code"] == code
    assert body["title"] and body["detail"]
    return body


class TestAppError:
    def test_class_defaults(self):
        error = NotFound()

        assert (error.status, error.code, error.detail) == (
            404,
            "not_found",
            "The resource was not found.",
        )
        assert error.headers == {}
        assert error.extensions == {}

    def test_detail_headers_and_extensions_per_instance(self):
        error = _ProbeConflict("Taken.", headers={"Retry-After": "3"}, retry_after=3)

        assert error.detail == "Taken."
        assert _ProbeConflict.detail == "The probe conflicts."
        assert error.headers == {"Retry-After": "3"}
        assert error.extensions == {"retry_after": 3}

    def test_unauthorized_always_sends_www_authenticate(self):
        assert Unauthorized().headers == {"WWW-Authenticate": "Bearer"}
        assert Unauthorized(headers={"X-Extra": "1"}).headers == {
            "WWW-Authenticate": "Bearer",
            "X-Extra": "1",
        }

    @pytest.mark.parametrize(
        ("error", "status", "code"),
        [
            (BadRequest, 400, "bad_request"),
            (Unauthorized, 401, "unauthorized"),
            (Forbidden, 403, "forbidden"),
            (NotFound, 404, "not_found"),
            (Conflict, 409, "conflict"),
        ],
    )
    def test_base_classes(self, error, status, code):
        assert issubclass(error, AppError)
        assert (error.status, error.code) == (status, code)


class TestProblemResponse:
    def test_renders_the_rfc_9457_members(self):
        response = problem_response(409, "user_exists", "Taken.")

        assert response.media_type == PROBLEM_JSON
        assert response.body == (
            b'{"type":"about:blank","title":"Conflict","status":409,'
            b'"detail":"Taken.","code":"user_exists"}'
        )

    def test_request_id_comes_from_the_structlog_context(self):
        structlog.contextvars.bind_contextvars(request_id="req-42")
        try:
            response = problem_response(400, "bad_request", "x")
        finally:
            structlog.contextvars.clear_contextvars()

        assert b'"request_id":"req-42"' in response.body

    def test_extensions_cannot_replace_standard_members(self):
        response = problem_response(
            400, "bad_request", "x", extensions={"status": 200, "code": "ok", "n": 1}
        )

        assert b'"status":400' in response.body
        assert b'"code":"bad_request"' in response.body
        assert b'"n":1' in response.body


class TestFieldErrors:
    def test_body_fields_drop_the_source_other_sources_keep_it(self):
        errors = [
            {"loc": ("body", "email"), "type": "value_error", "msg": "bad"},
            {"loc": ("body", "items", 0, "name"), "type": "missing", "msg": "m"},
            {"loc": ("body",), "type": "missing", "msg": "Field required"},
            {"loc": ("query", "limit"), "type": "less_than_equal", "msg": "q"},
            {"loc": ("header", "x-foo"), "type": "missing", "msg": "h"},
            {"loc": ("cookie", "refresh_token"), "type": "missing", "msg": "c"},
            {"loc": ("path", "item_id"), "type": "uuid_parsing", "msg": "p"},
        ]

        assert [e["field"] for e in field_errors(errors)] == [
            "email",
            "items.0.name",
            "body",
            "query.limit",
            "header.x-foo",
            "cookie.refresh_token",
            "path.item_id",
        ]

    def test_only_field_code_and_message_are_kept(self):
        """`input` and `ctx` can carry what the client sent (a password)."""
        [entry] = field_errors(
            [
                {
                    "loc": ("body", "password"),
                    "type": "string_too_short",
                    "msg": "String should have at least 8 characters",
                    "input": "hunter2",
                    "ctx": {"min_length": 8},
                    "url": "https://errors.pydantic.dev/",
                }
            ]
        )

        assert entry == {
            "field": "password",
            "code": "string_too_short",
            "message": "String should have at least 8 characters",
        }


class TestProblemResponsesHelper:
    def test_groups_codes_per_status(self):
        responses = problem_responses(_ProbeConflict, Conflict, Unauthorized, 422)

        assert list(responses) == [401, 409, 422]
        assert responses[409]["description"] == "Conflict: `probe_conflict`, `conflict`"
        assert responses[422]["description"] == (
            "Unprocessable Content: `validation_error`"
        )
        assert responses[401]["content"] == {
            PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/ProblemDetail"}}
        }


class TestHandlers:
    def test_app_error(self, probe_client):
        r = probe_client.post("/probe/conflict")

        body = _assert_problem(r, 409, "probe_conflict")
        assert body["title"] == "Conflict"
        assert body["detail"] == "The probe conflicts."
        assert body["retry_after"] == 7
        assert r.headers["retry-after"] == "7"
        assert body["request_id"] == r.headers["x-request-id"]

    def test_client_request_id_is_echoed_in_header_and_body(self, probe_client):
        r = probe_client.post("/probe/conflict", headers={"X-Request-ID": "abc-123"})

        assert r.headers["x-request-id"] == "abc-123"
        assert r.json()["request_id"] == "abc-123"

    def test_validation_error_maps_body_query_and_header_fields(self, probe_client):
        r = probe_client.post(
            "/probe/items?limit=500",
            json={"items": [{"name": "a", "size": "big"}, {"size": 1}]},
            headers={"X-Probe": "not-a-number"},
        )

        body = _assert_problem(r, 422, "validation_error")
        assert body["request_id"] == r.headers["x-request-id"]
        assert {(e["field"], e["code"]) for e in body["errors"]} == {
            ("items.0.size", "int_parsing"),
            ("items.1.name", "missing"),
            ("query.limit", "less_than_equal"),
            ("header.x-probe", "int_parsing"),
        }
        assert all(set(e) == {"field", "code", "message"} for e in body["errors"])
        assert "big" not in r.text and "not-a-number" not in r.text

    def test_unknown_route_is_404_not_found(self, probe_client):
        r = probe_client.get("/no/such/route")

        body = _assert_problem(r, 404, "not_found")
        assert body["request_id"] == r.headers["x-request-id"]

    def test_wrong_method_is_405_with_allow_header(self, probe_client):
        r = probe_client.get("/api/v1/auth/login")

        body = _assert_problem(r, 405, "method_not_allowed")
        assert r.headers["allow"] == "POST"
        assert body["request_id"] == r.headers["x-request-id"]

    def test_missing_bearer_token_is_401_with_www_authenticate(self, probe_client):
        r = probe_client.get("/api/v1/users/me")

        _assert_problem(r, 401, "unauthorized")
        assert r.headers["www-authenticate"] == "Bearer"

    def test_unexpected_error_is_a_generic_500(self, probe_client, caplog):
        r = probe_client.get("/probe/crash", headers={"X-Request-ID": "crash-1"})

        body = _assert_problem(r, 500, "internal_error")
        assert body["detail"] == "An unexpected error occurred."
        assert _SECRET not in r.text
        assert "_LeakyError" not in r.text
        # Known limitation: Starlette renders this in ServerErrorMiddleware,
        # outside the user middleware, which binds the request id in a child
        # task. Header and body member are both absent, never invented.
        assert "x-request-id" not in r.headers
        assert "request_id" not in body
