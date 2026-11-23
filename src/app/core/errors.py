"""Errors: the `AppError` family, RFC 9457 handlers and database error inspection.

Services raise `AppError` subclasses; `register_error_handlers` turns them,
request validation errors and Starlette `HTTPException`s into
`application/problem+json` responses (RFC 9457) with a stable
machine-readable `code` and the request id. Unexpected exceptions are
rendered by the request middleware with `internal_error_response`, so the
500 carries the same headers as every other response.

PostgreSQL error text can contain row values (`DETAIL: Key (email)=(...)`),
which are personal data. Code that reacts to a database error or logs one
uses only the structured fields read here: the exception type, the SQLSTATE
and the constraint name. They come from psycopg 3's diagnostics
(`exc.orig.diag`), the only supported driver.
"""

import re
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any, cast

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import get_logger

logger = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"
_INTERNAL_ERROR_DETAIL = "An unexpected error occurred."


class AppError(Exception):
    """Base of every error a service raises on purpose.

    Subclasses set `status`, `code` and a default `detail`; `title` defaults
    to the HTTP reason phrase of `status`.

    Args:
        detail: Human-readable explanation of this occurrence; defaults to
            the class's `detail`.
        headers: Extra response headers, for example `Retry-After`.
        **extensions: Extra top-level members of the problem object.
    """

    status: int = 500
    code: str = "internal_error"
    title: str | None = None
    detail: str = _INTERNAL_ERROR_DETAIL

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        **extensions: object,
    ) -> None:
        if detail is not None:
            self.detail = detail
        self.headers = dict(headers or {})
        self.extensions = extensions
        super().__init__(self.detail)


class BadRequest(AppError):
    """400: the request is syntactically valid but cannot be processed."""

    status = 400
    code = "bad_request"
    detail = "The request cannot be processed."


class Unauthorized(AppError):
    """401: missing or invalid credentials; always sends `WWW-Authenticate`."""

    status = 401
    code = "unauthorized"
    detail = "Authentication required."

    def __init__(
        self,
        detail: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        **extensions: object,
    ) -> None:
        super().__init__(
            detail,
            headers={"WWW-Authenticate": "Bearer", **(headers or {})},
            **extensions,
        )


class Forbidden(AppError):
    """403: authenticated, but not allowed to do this."""

    status = 403
    code = "forbidden"
    detail = "You are not allowed to perform this action."


class NotFound(AppError):
    """404: the resource does not exist (or is not visible to the caller)."""

    status = 404
    code = "not_found"
    detail = "The resource was not found."


class Conflict(AppError):
    """409: the request conflicts with the current state of a resource."""

    status = 409
    code = "conflict"
    detail = "The request conflicts with the current state of the resource."


class RateLimitedError(AppError):
    """429: a rate limit refused the request; `Retry-After` says for how long.

    Args:
        retry_after: Whole seconds after which the same request is accepted
            again (RFC 9110 section 10.2.3).
    """

    status = 429
    code = "rate_limited"
    detail = "Too many requests. Try again later."

    def __init__(self, retry_after: int) -> None:
        super().__init__(headers={"Retry-After": str(retry_after)})


class FieldError(BaseModel):
    """One invalid input of a 422 problem."""

    field: str = Field(
        description="Body field path (`email`, `items.0.name`) or "
        "`<source>.<name>` for other sources (`query.limit`)."
    )
    code: str = Field(description="Pydantic error type, e.g. `missing`.")
    message: str


class ProblemDetail(BaseModel):
    """RFC 9457 problem details as returned by every error response."""

    model_config = ConfigDict(extra="allow")

    type: str = "about:blank"
    title: str
    status: int
    detail: str
    code: str = Field(description="Stable machine-readable error code.")
    request_id: str | None = Field(
        default=None, description="Same value as the `X-Request-ID` header."
    )
    errors: list[FieldError] | None = Field(
        default=None, description="Only on 422 `validation_error`."
    )


def problem_responses(
    *errors: int | type[AppError],
) -> dict[int | str, dict[str, Any]]:
    """Build a route's `responses=` entries for the errors it can produce.

    Args:
        *errors: `AppError` subclasses, or a bare status (`422` documents
            `validation_error`).

    Returns:
        One `application/problem+json` response per status; its description
        lists the possible `code` values.
    """
    codes: dict[int, list[str]] = {}
    for error in errors:
        if isinstance(error, int):
            status = error
            code = (
                "validation_error"
                if error == HTTPStatus.UNPROCESSABLE_CONTENT
                else _code_for_status(error)
            )
        else:
            status, code = error.status, error.code
        codes.setdefault(status, [])
        if code not in codes[status]:
            codes[status].append(code)
    return {
        status: {
            "description": f"{HTTPStatus(status).phrase}: "
            + ", ".join(f"`{code}`" for code in status_codes),
            "content": {
                PROBLEM_JSON: {"schema": {"$ref": "#/components/schemas/ProblemDetail"}}
            },
        }
        for status, status_codes in sorted(codes.items())
    }


def _code_for_status(status: int) -> str:
    """`code` for a bare status: the reason phrase in snake case."""
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        return "http_error"
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


def problem_response(
    status: int,
    code: str,
    detail: str,
    *,
    title: str | None = None,
    headers: Mapping[str, str] | None = None,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    """Render an RFC 9457 problem response.

    `request_id` comes from the structlog context the request middleware
    binds; it is omitted when there is none. Extensions cannot replace the
    standard members.
    """
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": title or HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "code": code,
    }
    request_id = structlog.contextvars.get_contextvars().get("request_id")
    if request_id:
        body["request_id"] = request_id
    for name, value in jsonable_encoder(extensions or {}).items():
        body.setdefault(name, value)
    return JSONResponse(
        body, status_code=status, headers=headers, media_type=PROBLEM_JSON
    )


def field_errors(errors: Sequence[Any]) -> list[dict[str, str]]:
    """Map pydantic/FastAPI validation errors onto `errors[]` entries.

    Only `loc`, `type` and `msg` are used: `input` and `ctx` can echo what
    the client sent (a password, for example).
    """
    result: list[dict[str, str]] = []
    for error in errors:
        loc = [str(part) for part in error.get("loc", ())]
        if loc and loc[0] == "body":
            loc = loc[1:] or ["body"]
        result.append(
            {
                "field": ".".join(loc),
                "code": str(error.get("type", "value_error")),
                "message": str(error.get("msg", "")),
            }
        )
    return result


async def _app_error_handler(_request: Request, exc: AppError) -> Response:
    return problem_response(
        exc.status,
        exc.code,
        exc.detail,
        title=exc.title,
        headers=exc.headers,
        extensions=exc.extensions,
    )


async def _validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> Response:
    return problem_response(
        422,
        "validation_error",
        "The request contains invalid input.",
        extensions={"errors": field_errors(exc.errors())},
    )


async def _http_exception_handler(
    _request: Request, exc: StarletteHTTPException
) -> Response:
    # Same rule as Starlette's own handler: these statuses carry no body.
    if exc.status_code in {204, 304}:
        return Response(status_code=exc.status_code, headers=exc.headers)
    # Starlette annotates `detail` as `str`, but FastAPI's `HTTPException`
    # accepts any JSON value; only a string becomes the problem's `detail`.
    raw_detail = cast("object", exc.detail)
    detail = raw_detail if isinstance(raw_detail, str) else None
    return problem_response(
        exc.status_code,
        _code_for_status(exc.status_code),
        detail or HTTPStatus(exc.status_code).phrase,
        headers=exc.headers,
    )


def internal_error_response() -> JSONResponse:
    """Render the generic 500 problem; it never contains exception details."""
    return problem_response(500, "internal_error", _INTERNAL_ERROR_DETAIL)


_BODY_TOO_LARGE_DETAIL = "The request body is larger than the server accepts."


class BodyTooLargeError(StarletteHTTPException):
    """413: the request body grew past the limit while it was being read.

    An `HTTPException` on purpose: FastAPI's body parsing passes those on
    unchanged (it turns any other error into a 400), and the handler above
    renders it as a problem with the framework code `content_too_large`.
    """

    def __init__(self) -> None:
        super().__init__(status_code=413, detail=_BODY_TOO_LARGE_DETAIL)


def body_too_large_response() -> JSONResponse:
    """Render the 413 problem of a body above the limit, before any routing."""
    return problem_response(413, _code_for_status(413), _BODY_TOO_LARGE_DETAIL)


def register_error_handlers(app: FastAPI) -> None:
    """Install the problem-details handlers and the `ProblemDetail` schema.

    No handler is registered for `Exception`: Starlette would run it in
    `ServerErrorMiddleware`, outside the user middleware, so the 500 would
    miss the request id and the security headers. The request middleware
    renders it instead.

    Routes reference `#/components/schemas/ProblemDetail` through
    `problem_responses`; FastAPI adds only schemas that a route uses as a
    model, so the schema is added to the generated document here (the
    "Extending OpenAPI" pattern of the FastAPI docs).
    """
    # The decorator form accepts handlers typed for their exception class.
    app.exception_handler(AppError)(_app_error_handler)
    app.exception_handler(RequestValidationError)(_validation_error_handler)
    app.exception_handler(StarletteHTTPException)(_http_exception_handler)

    generate_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is None:
            schema = generate_openapi()
            definitions = ProblemDetail.model_json_schema(
                ref_template="#/components/schemas/{model}"
            )
            components = schema.setdefault("components", {}).setdefault("schemas", {})
            components.update(definitions.pop("$defs", {}))
            components["ProblemDetail"] = definitions
        return app.openapi_schema or {}

    # Assigning `app.openapi` is FastAPI's documented extension point; mypy
    # treats it as a method assignment.
    app.openapi = openapi  # type: ignore[method-assign]


def _driver_error(exc: DBAPIError) -> object:
    return exc.orig


def constraint_name(exc: DBAPIError) -> str | None:
    """Name of the violated constraint, or None if the driver did not report one."""
    # psycopg 3: Diagnostic object on the exception.
    diag = getattr(_driver_error(exc), "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) and name else None


def sqlstate(exc: DBAPIError) -> str | None:
    """SQLSTATE code of the error (e.g. `23505` unique violation)."""
    code = getattr(_driver_error(exc), "sqlstate", None)
    return code if isinstance(code, str) and code else None


def find_db_error(exc: BaseException) -> DBAPIError | None:
    """Return the first database error in `exc`'s cause/context chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, DBAPIError):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


def log_unexpected_error(
    log: structlog.stdlib.BoundLogger, event: str, exc: BaseException
) -> None:
    """Log an unexpected exception at error level without its message text.

    Exception messages can carry personal data (a PostgreSQL `DETAIL` line
    echoes the conflicting email or username), so only `error_type` is
    logged. For database errors the SQLSTATE and constraint name are added
    and the traceback is left out, because rendering it would print the
    driver's message again. Other errors keep the traceback.
    """
    db_error = find_db_error(exc)
    if db_error is None:
        log.error(event, error_type=type(exc).__name__, exc_info=exc)
        return
    log.error(
        event,
        error_type=type(exc).__name__,
        db_error_type=type(db_error).__name__,
        sqlstate=sqlstate(db_error),
        constraint=constraint_name(db_error),
    )
