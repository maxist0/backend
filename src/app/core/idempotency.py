"""Idempotency keys: a retried request with the same key is answered, not repeated.

A route opts in with the `Idempotent` marker from `app.modules.users`, which
is `idempotent(scope=...)` bound to the signed-in user. The contract follows
draft-ietf-httpapi-idempotency-key-header; the design is ADR 0014. Without
the header a request runs as usual.

The guard inserts the key row `(scope, key, fingerprint)` in the request's
own session before the route runs, so the row commits with the service's
commit or not at all: a request never runs twice, not even after a crash. A
parallel request with the same key waits on the unique index for at most
500 ms and then gets 409. The request middleware hands every response
message to a recorder; after the route the guard stores the response on the
row and commits, which also commits the row of a route that wrote nothing.

A response is stored only if the row was committed and the status is below
500 and not 429 (`is_storable_status`). An error that rolled everything back
leaves no row, so a retry runs again. A committed row that has had no
response for `_IN_FLIGHT_SECONDS` belongs to a request whose outcome is
unknown: a retry gets 409 and never runs it again.
"""

import hashlib
import hmac
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import timedelta
from http import HTTPStatus
from typing import Annotated, Any, NoReturn

from fastapi import Depends, FastAPI, Request, Response, params
from fastapi.dependencies.models import Dependant
from fastapi.routing import iter_route_contexts
from prometheus_client import Counter
from sqlalchemy import (
    CheckConstraint,
    Index,
    LargeBinary,
    SmallInteger,
    String,
    UniqueConstraint,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column
from starlette.routing import BaseRoute
from starlette.types import Message
from taskiq import TaskiqDepends

from app.core.config import Settings, get_settings
from app.core.database import Base, CreatedAt, DbSession, UUIDPrimaryKey
from app.core.errors import (
    AppError,
    BadRequest,
    Conflict,
    RateLimitedError,
    log_unexpected_error,
    problem_responses,
    render_handled_error,
    sqlstate,
)
from app.core.logging import get_logger
from app.core.middleware import RESPONSE_RECORDER, route_template
from app.core.security import derive_key
from app.core.tasks import TaskDbSession, task

logger = get_logger(__name__)

IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
REPLAYED_HEADER = "Idempotent-Replayed"

# Visible ASCII without quotes, backslash, comma or space, so the value is the
# same as a bare token and inside a Structured Field string. At least 16
# characters: in a route scope all anonymous clients share the key space,
# and a short key could be guessed (draft section 5). A UUID has 36.
_KEY = re.compile(r"[A-Za-z0-9+/=_.:~-]{16,255}")
_CODE = re.compile(r"`([a-z0-9_]+)`")
_FINGERPRINT_LABEL = b"idempotency fingerprint key v1"

# How long a request waits for a parallel one with the same key before it
# gets 409. Longer waits rarely end in a replay (the first request stores its
# response only after its commit) but keep a pooled connection busy.
_SET_LOCK_TIMEOUT = text("SET LOCAL lock_timeout = '500ms'")
# Back to the session's value (server, role or connection options), so the
# service's own lock waits are not cut short.
_RESET_LOCK_TIMEOUT = text("SET LOCAL lock_timeout TO DEFAULT")
_LOCK_NOT_AVAILABLE = "55P03"

# A committed row without response older than this belongs to a request that
# will not finish: no request runs that long between its first commit and
# storing its response.
_IN_FLIGHT_SECONDS = 60
_MAX_STORED_BODY = 64 * 1024
# Response headers a replay repeats; everything else is per response (the
# middleware's headers) or must never be repeated (`Set-Cookie`).
_STORED_HEADERS = frozenset({b"content-type", b"location", b"www-authenticate"})

CLEANUP_BATCH_SIZE = 1000
CLEANUP_MAX_BATCHES = 100

REQUESTS = Counter(
    "idempotency_requests_total",
    "Requests with an Idempotency-Key, by outcome.",
    ["outcome"],
)
for _outcome in (
    "stored",
    "not_stored",
    "replayed",
    "in_use",
    "outcome_unknown",
    "reused",
    "invalid",
):
    REQUESTS.labels(outcome=_outcome)
# Counted in the worker. A capped run leaves rows for the next hour; runs that
# keep hitting the cap mean keys arrive faster than the cleanup deletes them.
CLEANUP_CAPPED = Counter(
    "idempotency_cleanup_capped_total",
    "Cleanup runs that stopped at the batch cap with expired keys left.",
)


class IdempotencyKey(UUIDPrimaryKey, CreatedAt, Base):
    """A used idempotency key and, once known, the response to replay."""

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("scope", "key"),
        Index("ix_idempotency_keys_created_at", "created_at"),
        CheckConstraint(
            "response_status BETWEEN 100 AND 599", name="response_status_range"
        ),
        CheckConstraint(
            "(response_status IS NULL) = (response_headers IS NULL) "
            "AND (response_status IS NULL) = (response_body IS NULL)",
            name="response_complete",
        ),
    )

    # `user:<uuid>` or `route:<METHOD> <path template>`.
    scope: Mapped[str] = mapped_column(String(300))
    key: Mapped[str] = mapped_column(String(255))
    # HMAC-SHA256 of the request; see `request_fingerprint`.
    fingerprint: Mapped[bytes] = mapped_column(LargeBinary)
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    # `[[name, value], ...]` of the headers in `_STORED_HEADERS`.
    response_headers: Mapped[list[list[str]] | None] = mapped_column(JSONB)
    response_body: Mapped[bytes | None] = mapped_column(LargeBinary)


class IdempotencyKeyInvalidError(BadRequest):
    """400: the `Idempotency-Key` header is malformed or sent twice."""

    code = "idempotency_key_invalid"
    detail = (
        "The Idempotency-Key header must be one value of 16 to 255 characters "
        "(letters, digits and + / = _ . : ~ -), optionally in double quotes."
    )


class IdempotencyKeyInUseError(Conflict):
    """409: a request with this key is still being processed."""

    code = "idempotency_key_in_use"
    detail = "A request with this Idempotency-Key is still being processed."

    def __init__(self) -> None:
        super().__init__(headers={"Retry-After": "1"})


class IdempotencyOutcomeUnknownError(Conflict):
    """409: a request with this key ran, but its response is not available."""

    code = "idempotency_outcome_unknown"
    detail = (
        "A request with this Idempotency-Key was processed, but its result is "
        "not available. Do not retry with this key; read the current state."
    )


class IdempotencyKeyReusedError(AppError):
    """422: the key was used before for a different request."""

    status = 422
    code = "idempotency_key_reused"
    detail = "This Idempotency-Key was used for a different request."


class IdempotentReplayError(Exception):
    """Carries a stored response to the handler that sends it.

    Raised by the guard before the route runs; not an error of the request.
    """

    def __init__(self, response: Response) -> None:
        super().__init__("replay")
        self.response = response


def parse_key(values: Sequence[str]) -> str | None:
    """The key of an `Idempotency-Key` header, or None without the header.

    Accepts the draft's Structured Field string (`"<key>"`) and a bare token
    (`<key>`); both mean the same key. Keys compare exactly.

    Args:
        values: Every value of the header in the request.

    Raises:
        IdempotencyKeyInvalidError: The header is repeated, empty or not a
            key of the documented format.
    """
    if not values:
        return None
    if len(values) > 1:
        raise IdempotencyKeyInvalidError()
    value = values[0]
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':  # noqa: PLR2004 - the two quotes
        value = value[1:-1]
    if not _KEY.fullmatch(value):
        raise IdempotencyKeyInvalidError()
    return value


def request_fingerprint(
    secret: str, method: str, path: str, query: bytes, body: bytes
) -> bytes:
    """HMAC-SHA256 of a request, to tell a retry from another request.

    Each part is length-prefixed, so no two different requests share an
    input. The path is included because the same key on another object or
    route is another request. The HMAC key is derived from `secret`
    (`JWT_SECRET_KEY`) for this purpose only; without it a stored
    fingerprint cannot be tested against guessed passwords.
    """
    mac = hmac.new(derive_key(secret, _FINGERPRINT_LABEL), digestmod=hashlib.sha256)
    for part in (method.encode(), path.encode(errors="surrogateescape"), query, body):
        mac.update(len(part).to_bytes(8, "big"))
        mac.update(part)
    return mac.digest()


class RecordedResponse:
    """Copies the response messages the request middleware passes on.

    Keeps the status, the headers and up to `_MAX_STORED_BODY` of body; a
    larger body is dropped and marked `too_large`.
    """

    def __init__(self) -> None:
        self.status: int | None = None
        self.headers: list[tuple[bytes, bytes]] = []
        self.body = b""
        self.complete = False
        self.too_large = False

    def record(self, message: Message) -> None:
        """Take one `http.response.*` message."""
        if message["type"] == "http.response.start":
            self.status = message["status"]
            self.headers = list(message.get("headers", ()))
        elif message["type"] == "http.response.body" and not self.complete:
            if not self.too_large:
                self.body += message.get("body", b"")
                if len(self.body) > _MAX_STORED_BODY:
                    self.too_large = True
                    self.body = b""
            self.complete = not message.get("more_body", False)


def is_storable_status(status: int) -> bool:
    """Whether a response with `status` may be stored for replay.

    Not a 5xx: the outcome is unknown, and a replay would claim a failure
    that may not be one. Not a 429: a limit refuses before the work, so the
    retry must run.
    """
    return status < HTTPStatus.INTERNAL_SERVER_ERROR and (
        status != HTTPStatus.TOO_MANY_REQUESTS
    )


def replayed_headers(raw: Sequence[tuple[bytes, bytes]]) -> list[list[str]]:
    """The headers of `raw` a replay repeats (`_STORED_HEADERS`), as strings."""
    return [
        [name.decode("latin-1"), value.decode("latin-1")]
        for name, value in raw
        if name.lower() in _STORED_HEADERS
    ]


def sets_cookie(raw: Sequence[tuple[bytes, bytes]]) -> bool:
    """Whether `raw` holds a `Set-Cookie`; such a response is never stored."""
    return any(name.lower() == b"set-cookie" for name, _ in raw)


async def _claim(
    db: AsyncSession, scope: str, key: str, fingerprint: bytes
) -> uuid.UUID | None:
    """Insert the key row; None if the key exists already (committed).

    Raises:
        IdempotencyKeyInUseError: A parallel request holds the key longer
            than the lock timeout.
    """
    statement = (
        pg_insert(IdempotencyKey)
        .values(id=uuid.uuid7(), scope=scope, key=key, fingerprint=fingerprint)
        .on_conflict_do_nothing(index_elements=["scope", "key"])
        .returning(IdempotencyKey.id)
    )
    await db.execute(_SET_LOCK_TIMEOUT)
    try:
        row_id = (await db.execute(statement)).scalar_one_or_none()
    except DBAPIError as exc:
        if sqlstate(exc) != _LOCK_NOT_AVAILABLE:
            raise
        # Also undoes the SET LOCAL.
        await db.rollback()
        REQUESTS.labels(outcome="in_use").inc()
        raise IdempotencyKeyInUseError() from None
    await db.execute(_RESET_LOCK_TIMEOUT)
    return row_id


async def _answer_existing(
    db: AsyncSession, scope: str, key: str, fingerprint: bytes
) -> NoReturn:
    """Answer a request whose key is taken: replay, 422 or 409."""
    recent = IdempotencyKey.created_at > func.statement_timestamp() - timedelta(
        seconds=_IN_FLIGHT_SECONDS
    )
    result = await db.execute(
        select(
            IdempotencyKey.fingerprint,
            IdempotencyKey.response_status,
            IdempotencyKey.response_headers,
            IdempotencyKey.response_body,
            recent,
        ).where(IdempotencyKey.scope == scope, IdempotencyKey.key == key)
    )
    row = result.one_or_none()
    if row is None:
        # Deleted since the insert saw it (cleanup, or a 429 of the first).
        REQUESTS.labels(outcome="in_use").inc()
        raise IdempotencyKeyInUseError()
    stored_fingerprint, status, headers, body, is_recent = row
    if not hmac.compare_digest(stored_fingerprint, fingerprint):
        REQUESTS.labels(outcome="reused").inc()
        raise IdempotencyKeyReusedError()
    if status is not None and headers is not None and body is not None:
        REQUESTS.labels(outcome="replayed").inc()
        logger.info("idempotency.replayed", status=status)
        response = Response(content=body, status_code=status)
        for name, value in headers:
            response.headers.append(name, value)
        response.headers[REPLAYED_HEADER] = "true"
        raise IdempotentReplayError(response)
    if is_recent:
        REQUESTS.labels(outcome="in_use").inc()
        raise IdempotencyKeyInUseError()
    REQUESTS.labels(outcome="outcome_unknown").inc()
    logger.warning("idempotency.outcome_unknown")
    raise IdempotencyOutcomeUnknownError()


async def _store(
    db: AsyncSession,
    row_id: uuid.UUID,
    status: int,
    headers: list[list[str]],
    body: bytes,
) -> bool:
    """Store the response on the row and commit; False if the row is gone."""
    result = await db.execute(
        update(IdempotencyKey)
        .where(IdempotencyKey.id == row_id)
        .values(response_status=status, response_headers=headers, response_body=body)
        .returning(IdempotencyKey.id)
        .execution_options(synchronize_session=False)
    )
    stored = result.scalar_one_or_none() is not None
    await db.commit()
    return stored


async def _finish_after_error(
    db: AsyncSession, row_id: uuid.UUID, exc: Exception
) -> None:
    """Store the error response if the row was committed before the error.

    The rollback discards what the error left uncommitted, as `get_db`
    would. The statements below then find the row only if an earlier
    commit of the request (a refusal's counters, a password confirmation)
    committed it: such a request must not run again.
    """
    try:
        await db.rollback()
        if isinstance(exc, RateLimitedError):
            # Delete a committed row, so the retry runs again.
            await db.execute(
                delete(IdempotencyKey)
                .where(IdempotencyKey.id == row_id)
                .execution_options(synchronize_session=False)
            )
            await db.commit()
            REQUESTS.labels(outcome="not_stored").inc()
            return
        response = render_handled_error(exc)
        if response is None or not is_storable_status(response.status_code):
            REQUESTS.labels(outcome="not_stored").inc()
            return
        stored = await _store(
            db,
            row_id,
            response.status_code,
            replayed_headers(response.raw_headers),
            bytes(response.body),
        )
        REQUESTS.labels(outcome="stored" if stored else "not_stored").inc()
    except DBAPIError as store_exc:
        # The caller re-raises the original error; this one is only logged.
        log_unexpected_error(logger, "idempotency.store_failed", store_exc)
        await db.rollback()
        REQUESTS.labels(outcome="not_stored").inc()


def _unstorable(db: AsyncSession, recorder: RecordedResponse) -> str | None:
    """Why a recorded response must not be stored, or None."""
    if recorder.too_large:
        return "too_large"
    if recorder.status is None or not recorder.complete:
        return "incomplete"
    if sets_cookie(recorder.headers):
        return "set_cookie"
    if db.new or db.dirty or db.deleted:
        # The guard's commit would write them although the route did not.
        return "uncommitted_changes"
    return None


async def _finish_after_success(
    db: AsyncSession, row_id: uuid.UUID, recorder: RecordedResponse
) -> None:
    """Store the recorded response and commit (the row too, if still open)."""
    reason = _unstorable(db, recorder)
    if reason is not None:
        logger.error("idempotency.response_not_stored", reason=reason)
    status = recorder.status
    if reason is not None or status is None or not is_storable_status(status):
        # A row the service committed stays without response (outcome
        # unknown); one it did not commit disappears, and a retry runs again.
        await db.rollback()
        REQUESTS.labels(outcome="not_stored").inc()
        return
    try:
        stored = await _store(
            db, row_id, status, replayed_headers(recorder.headers), recorder.body
        )
        if not stored:
            # The service rolled back the request's transaction on its success
            # path and with it the key row; this request was not protected.
            logger.error("idempotency.key_row_lost")
    except DBAPIError as exc:
        # The response is on its way already; a retry gets 409.
        log_unexpected_error(logger, "idempotency.store_failed", exc)
        await db.rollback()
        stored = False
    REQUESTS.labels(outcome="stored" if stored else "not_stored").inc()


# The guard functions `idempotent` created; the OpenAPI step and the tests
# find marked routes by them.
_GUARDS: set[Callable[..., object]] = set()


def idempotent(
    *, scope: Callable[..., str | Awaitable[str | None] | None]
) -> params.Depends:
    """A route dependency that makes a route accept `Idempotency-Key`.

    Modules use the bound marker `Idempotent` from `app.modules.users`,
    listed after the route's rate limits:

        @router.post("", dependencies=[rate_limit(...), Idempotent], ...)

    The service is unchanged: it commits once, and the key row commits with
    it. Because the guard commits after the route, a service of such a
    route must not roll back the request's transaction on its success path
    (end a read transaction with `commit()`) and must not leave writes
    uncommitted on purpose.

    Args:
        scope: A FastAPI dependency returning the caller's scope id (the
            user id), or None for an anonymous request, which is scoped by
            route.

    Returns:
        The dependency for the route's `dependencies=[...]`.
    """

    async def idempotency_guard(
        request: Request,
        db: DbSession,
        owner: Annotated[str | None, Depends(scope)],
    ) -> AsyncIterator[None]:
        try:
            key = parse_key(request.headers.getlist(IDEMPOTENCY_KEY_HEADER))
        except IdempotencyKeyInvalidError:
            REQUESTS.labels(outcome="invalid").inc()
            raise
        if key is None:
            yield
            return
        if owner is not None:
            key_scope = f"user:{owner}"
        else:
            template = route_template(request.app, request.scope.get("route"))
            key_scope = f"route:{request.method} {template}"
        fingerprint = request_fingerprint(
            get_settings().JWT_SECRET_KEY.get_secret_value(),
            request.method,
            request.scope["path"],
            request.scope.get("query_string", b""),
            await request.body(),
        )
        row_id = await _claim(db, key_scope, key, fingerprint)
        if row_id is None:
            await _answer_existing(db, key_scope, key, fingerprint)
        recorder = RecordedResponse()
        request.scope[RESPONSE_RECORDER] = recorder
        try:
            yield
        except Exception as exc:
            await _finish_after_error(db, row_id, exc)
            raise
        await _finish_after_success(db, row_id, recorder)

    _GUARDS.add(idempotency_guard)
    dependency: params.Depends = Depends(idempotency_guard)
    return dependency


def marks_idempotent(dependant: Dependant) -> bool:
    """Whether a route's dependency tree contains an `idempotent` guard."""
    if dependant.call in _GUARDS:
        return True
    return any(marks_idempotent(sub) for sub in dependant.dependencies)


def _header_parameter(retention_hours: int) -> dict[str, Any]:
    # Any: an OpenAPI parameter object is arbitrary JSON.
    return {
        "name": IDEMPOTENCY_KEY_HEADER,
        "in": "header",
        "required": False,
        "schema": {"type": "string"},
        "description": (
            "Optional. A unique value per intended operation (a UUID), 16 to 255 "
            "characters of letters, digits and `+ / = _ . : ~ -`, bare or as a "
            "quoted string. A retry with the same key and the same request gets "
            "the stored response with `Idempotent-Replayed: true` instead of "
            f"running again. Keys are kept for {retention_hours} hours; use a new "
            "key whenever the request changes."
        ),
    }


def merge_error_responses(operation: dict[str, Any]) -> None:
    """Add the idempotency problem codes to an OpenAPI operation's responses.

    A status the route documents already gets the codes appended to its
    description; a new status gets a `problem_responses` entry.

    Args:
        operation: The operation object, changed in place. Any: OpenAPI
            JSON is arbitrary.
    """
    responses: dict[str, Any] = operation.setdefault("responses", {})
    for error in (
        IdempotencyKeyInvalidError,
        IdempotencyKeyInUseError,
        IdempotencyOutcomeUnknownError,
        IdempotencyKeyReusedError,
    ):
        entry = responses.setdefault(
            str(error.status), problem_responses(error)[error.status]
        )
        codes = _CODE.findall(entry.get("description", ""))
        if error.code not in codes:
            codes.append(error.code)
        entry["description"] = f"{HTTPStatus(error.status).phrase}: " + ", ".join(
            f"`{code}`" for code in codes
        )
    operation["responses"] = dict(sorted(responses.items()))


def document_idempotent_routes(
    schema: dict[str, Any], routes: Sequence[BaseRoute], *, retention_hours: int
) -> None:
    """Add the header and the idempotency errors to every marked operation.

    Modules write only the marker; this keeps their OpenAPI complete.

    Args:
        schema: The generated OpenAPI document, changed in place.
        routes: The app's routes.
        retention_hours: `IDEMPOTENCY_RETENTION_HOURS`, stated in the
            header's description.
    """
    paths: dict[str, Any] = schema.get("paths", {})
    for context in iter_route_contexts(routes):
        dependant: Dependant | None = getattr(context, "dependant", None)
        path = context.path_format
        if dependant is None or path is None or not marks_idempotent(dependant):
            continue
        for method in sorted(context.methods or ()):
            operation = paths.get(path, {}).get(method.lower())
            if operation is None:
                continue
            operation.setdefault("parameters", []).append(
                _header_parameter(retention_hours)
            )
            merge_error_responses(operation)


async def _replay(_request: Request, exc: IdempotentReplayError) -> Response:
    return exc.response


def register_idempotency(app: FastAPI, settings: Settings) -> None:
    """Install the replay handler and the OpenAPI step on `app`.

    Called by `create_app` after `register_error_handlers`.

    Args:
        app: The application.
        settings: Its settings; the OpenAPI text states the retention.
    """
    app.exception_handler(IdempotentReplayError)(_replay)
    generate_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        first = app.openapi_schema is None
        schema = generate_openapi()
        if first:
            document_idempotent_routes(
                schema,
                app.routes,
                retention_hours=settings.IDEMPOTENCY_RETENTION_HOURS,
            )
        return schema

    # Assigning `app.openapi` is FastAPI's documented extension point.
    app.openapi = openapi  # type: ignore[method-assign]


async def delete_expired_keys(
    db: AsyncSession, retention: timedelta, *, batch_size: int, max_batches: int
) -> tuple[int, bool]:
    """Delete keys older than `retention`, one transaction per batch.

    Rows locked by a request storing its response are skipped (`SKIP
    LOCKED`) and go in the next run.

    Returns:
        The number of deleted rows, and whether the run hit `max_batches`.
    """
    total = 0
    for _ in range(max_batches):
        batch = (
            select(IdempotencyKey.id)
            .where(IdempotencyKey.created_at < func.now() - retention)
            .order_by(IdempotencyKey.created_at)
            .limit(batch_size)
            .with_for_update(skip_locked=True)
        )
        result = await db.execute(
            delete(IdempotencyKey)
            .where(IdempotencyKey.id.in_(batch.scalar_subquery()))
            .returning(IdempotencyKey.id)
            .execution_options(synchronize_session=False)
        )
        deleted = len(result.all())
        await db.commit()
        total += deleted
        if deleted < batch_size:
            return total, False
    return total, True


@task(
    task_name="maintenance.idempotency_cleanup",
    retry_on_error=True,
    schedule=[{"cron": "23 * * * *"}],
)
async def idempotency_cleanup_task(db: TaskDbSession = TaskiqDepends()) -> None:
    """Delete idempotency keys past `IDEMPOTENCY_RETENTION_HOURS`, hourly.

    At most `CLEANUP_MAX_BATCHES` batches of `CLEANUP_BATCH_SIZE` rows per
    run, like `auth.cleanup`; the next run continues. A capped run logs at
    warning level and counts `idempotency_cleanup_capped_total`. Idempotent:
    a second run, in parallel or later, finds only what the first left.

    Args:
        db: The task's database session.
    """
    deleted, capped = await delete_expired_keys(
        db,
        timedelta(hours=get_settings().IDEMPOTENCY_RETENTION_HOURS),
        batch_size=CLEANUP_BATCH_SIZE,
        max_batches=CLEANUP_MAX_BATCHES,
    )
    if capped:
        CLEANUP_CAPPED.inc()
    log = logger.warning if capped else logger.info
    log("idempotency.cleanup_done", deleted=deleted, capped=capped)
