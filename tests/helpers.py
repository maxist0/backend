"""Helpers shared by the test modules.

Both suites use the paths, the event loop helpers, `build_settings`, the
log readers and the module content readers (`module_tables`,
`declared_tasks`). The rest serves the integration tests: HTTP flows, captured
mails, the SQL helper, admin requests, Redis dumps and parallel runners.
"""

import asyncio
import html
import os
import re
import sys
import threading
import traceback
import uuid
from collections.abc import Awaitable, Callable, Coroutine, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
import redis as redis_sync
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi_mail import MessageSchema
from httpx2 import Response
from sqlalchemy import Engine, RowMapping, Table, text
from starlette.types import ASGIApp
from taskiq import AsyncTaskiqDecoratedTask, AsyncTaskiqTask
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.core import Base, Module
from app.core.config import Settings, get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]

# Same pins as compose.dev.yaml (tests/unit/core/test_dev_compose_parity.py
# fails when they drift): same major as production.
POSTGRES_IMAGE = (
    "postgres:18.7-alpine3.24"
    "@sha256:b6972e1115a4ff8eba5d7f7d19184926d1e16d7d1136ae1e1a7ebf286da0f015"
)
REDIS_IMAGE = (
    "redis:7.4.12-alpine3.22"
    "@sha256:7f7d30b7fdd09467bac58198a1f166dd69789d348d4fe859cccb20e0cf287fbe"
)

PASSWORD = "Password123!"
NEW_PASSWORD = "NewPassword456!"

# Marker values that must never reach a log line or Redis.
PII_USERNAME = "zebra_user_91"
PII_EMAIL = "zebra.mail91@example.com"

# The lockout defaults the suite runs with (Settings defaults; no test
# environment overrides them).
MAXIMUM_LOGIN_ATTEMPTS: int = Settings.model_fields["LOGIN_MAX_ATTEMPTS"].default
LOGIN_LOCKOUT_SECONDS: int = Settings.model_fields["LOGIN_LOCKOUT_SECONDS"].default

# psycopg's async mode cannot run on Windows' default ProactorEventLoop (the app
# refuses to start on it, see app.core.database.ensure_supported_event_loop).
# Event loops are therefore created through this factory instead of the
# deprecated `WindowsSelectorEventLoopPolicy`; `None` means the platform
# default.
LOOP_FACTORY: Callable[[], asyncio.AbstractEventLoop] | None = (
    asyncio.SelectorEventLoop if sys.platform == "win32" else None
)

type SettingsFactory = Callable[..., Settings]


def build_settings(**overrides: object) -> Settings:
    """Settings for one test: `unittest`, no admin panel.

    `Settings` reads no dotenv file (only `get_settings` adds `.env.local`
    in dev), so the result depends on the arguments and the environment.

    The admin stays off unless overridden: sqladmin keeps the session factory
    on its view classes, so a second admin-enabled app would rebind the
    shared views. Every default can be overridden, `None` included.
    """
    # Any: `Settings` validates every field value itself, also wrong ones on
    # purpose; a precise type would have to repeat the field list.
    values: dict[str, Any] = {
        "ENVIRONMENT": "unittest",
        "ADMIN_ENABLED": False,
        **overrides,
    }
    return Settings(**values)


def new_test_client(
    app: ASGIApp,
    *,
    raise_server_exceptions: bool = True,
    follow_redirects: bool = True,
) -> TestClient:
    """`TestClient` whose anyio portal runs on `LOOP_FACTORY`.

    Starlette passes `backend_options` to `anyio.from_thread.start_blocking_portal`,
    and anyio's asyncio backend forwards `loop_factory` to `asyncio.Runner`.
    """
    return TestClient(
        app,
        backend_options={"loop_factory": LOOP_FACTORY},
        raise_server_exceptions=raise_server_exceptions,
        follow_redirects=follow_redirects,
    )


def app_of(client: TestClient) -> FastAPI:
    """The FastAPI app behind `client`."""
    assert isinstance(client.app, FastAPI)
    return client.app


def run_async[T](coro: Awaitable[T]) -> T:
    """Run `coro` to completion on a fresh loop made by `LOOP_FACTORY`.

    For tests that call async code outside the TestClient (own engines only,
    never the app engine, which lives on the TestClient's loop).
    """

    async def _wrapper() -> T:
        return await coro

    return asyncio.run(_wrapper(), loop_factory=LOOP_FACTORY)


def call_in_app[T](client: TestClient, func: Callable[[], Awaitable[T]]) -> T:
    """Run `func` on the event loop of an entered `client`.

    The app's engine, session factory and Redis pools live on that loop.
    """
    assert client.portal is not None, "enter the TestClient as a context manager"
    return client.portal.call(func)


def run_task[**P, R](
    client: TestClient,
    # Any: the send and yield types of the task's coroutine are irrelevant.
    task: AsyncTaskiqDecoratedTask[P, Coroutine[Any, Any, R]],
    *args: P.args,
    **kwargs: P.kwargs,
) -> AsyncTaskiqTask[R]:
    """Enqueue `task` on the client app's broker and return once it ran.

    With the `app` fixture's inline broker the task runs before this
    returns, on the app's event loop, with the app's task database.
    """

    async def _kiq() -> AsyncTaskiqTask[R]:
        return await task.kiq(*args, **kwargs)

    return call_in_app(client, _kiq)


def log_text(caplog: pytest.LogCaptureFixture) -> str:
    """Every captured record as text, formatted tracebacks included.

    Tracebacks carry exception messages, which must not leak data either.
    """
    parts: list[str] = []
    for record in caplog.records:
        parts.append(f"{record.name} {record.msg} {record.getMessage()}")
        if record.exc_text:
            parts.append(record.exc_text)
        if record.exc_info:
            parts.append("".join(traceback.format_exception(*record.exc_info)))
    return "\n".join(parts)


def log_events(
    caplog: pytest.LogCaptureFixture, name: str | None = None
) -> list[dict[str, Any]]:
    """The structlog event dicts captured, optionally only the event `name`.

    Any: the values of a structlog event are whatever the call site logged.
    """
    events: list[dict[str, Any]] = []
    for record in caplog.records:
        msg = record.msg
        if isinstance(msg, dict):
            event = cast("dict[str, Any]", msg)
            if name is None or event.get("event") == name:
                events.append(event)
    return events


def module_tables(module: Module) -> set[str]:
    """Names of the tables that `module.models` defines.

    These are the tables of the mapped classes defined in that file plus the
    plain `Table` objects at its top level (an association table, say).
    Models imported from another file do not count.
    """
    models = module.models
    if models is None:
        return set()
    tables = [
        mapper.local_table
        for mapper in Base.registry.mappers
        if mapper.class_.__module__ == models.__name__
    ]
    tables += [value for value in vars(models).values() if isinstance(value, Table)]
    return {table.name for table in tables if isinstance(table, Table)}


def declared_tasks(
    tasks: ModuleType | None,
) -> list[AsyncTaskiqDecoratedTask[..., object]]:
    """The taskiq tasks defined in `tasks`, not the ones imported into it.

    `app.core.task` keeps the function's `__module__` (`functools.wraps`),
    which tells where a task is defined.
    """
    if tasks is None:
        return []
    return [
        value
        for value in vars(tasks).values()
        if isinstance(value, AsyncTaskiqDecoratedTask)
        and value.original_func.__module__ == tasks.__name__
    ]


def register(
    client: TestClient,
    *,
    username: str,
    email: str | None = None,
    password: str = PASSWORD,
    display_name: str | None = None,
) -> Response:
    """POST /auth/register; the email defaults to `<username>@example.com`."""
    payload = {
        "username": username,
        "email": email or f"{username}@example.com",
        "password": password,
    }
    if display_name is not None:
        payload["display_name"] = display_name
    return client.post("/api/v1/auth/register", json=payload)


def register_ok(
    client: TestClient,
    *,
    username: str,
    email: str | None = None,
    password: str = PASSWORD,
    display_name: str | None = None,
) -> Response:
    """`register` that fails loudly unless the API answered 201.

    Callers that need the verification token use the `captured_emails`
    fixture: without it mail is not configured and nothing is sent.
    """
    r = register(
        client,
        username=username,
        email=email,
        password=password,
        display_name=display_name,
    )
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text[:200]}"
    return r


def login(
    client: TestClient,
    *,
    username: str,
    password: str = PASSWORD,
    headers: dict[str, str] | None = None,
) -> Response:
    """POST /auth/login with the OAuth2 password form."""
    return client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
        headers=headers or {},
    )


def bearer(access_token: str) -> dict[str, str]:
    """The `Authorization` header for an access token."""
    return {"Authorization": f"Bearer {access_token}"}


def register_and_login(
    client: TestClient,
    *,
    username: str = "alice",
    email: str | None = None,
    display_name: str | None = None,
) -> str:
    """Register a user, log in, and return the access token."""
    register_ok(client, username=username, email=email, display_name=display_name)
    r = login(client, username=username)
    assert r.status_code == 200, f"login failed: {r.text}"
    token: str = r.json()["access_token"]
    return token


@dataclass(frozen=True)
class Tokens:
    access: str
    refresh: str


def login_tokens(client: TestClient, username: str, password: str = PASSWORD) -> Tokens:
    """Log in and return the tokens, leaving the client's cookie jar EMPTY.

    Tests that juggle several sessions send the refresh token explicitly via
    `refresh_with`, so the shared jar can never hide which token was used.
    """
    r = login(client, username=username, password=password)
    assert r.status_code == 200, r.text
    refresh = client.cookies.get("refresh_token")
    assert refresh
    client.cookies.clear()
    return Tokens(access=r.json()["access_token"], refresh=refresh)


def refresh_with(
    client: TestClient, refresh_token: str
) -> tuple[Response, Tokens | None]:
    """POST /auth/refresh with an explicit refresh cookie.

    Returns the response and the new tokens (None unless 200). The jar is
    cleared afterwards.
    """
    r = client.post(
        "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={refresh_token}"}
    )
    new_refresh = client.cookies.get("refresh_token")
    client.cookies.clear()
    if r.status_code != 200:
        return r, None
    assert new_refresh
    return r, Tokens(access=r.json()["access_token"], refresh=new_refresh)


def change_password(
    client: TestClient,
    access: str | None,
    current: str = PASSWORD,
    new: str = "NewSecure456!",
) -> Response:
    """PUT /users/me/password; no `Authorization` header without a token."""
    return client.put(
        "/api/v1/users/me/password",
        json={"current_password": current, "new_password": new},
        headers=bearer(access) if access else {},
    )


def change_email(
    client: TestClient,
    access: str | None,
    new_email: str,
    password: str = PASSWORD,
) -> Response:
    """PUT /users/me/email; no `Authorization` header without a token."""
    return client.put(
        "/api/v1/users/me/email",
        json={"current_password": password, "new_email": new_email},
        headers=bearer(access) if access else {},
    )


def verify_email(client: TestClient, token: str) -> Response:
    """POST /auth/email/verify: redeem a verification or email-change token."""
    return client.post("/api/v1/auth/email/verify", json={"token": token})


def request_reset(
    client: TestClient,
    captured_emails: CapturedEmails,
    email: str = "alice@example.com",
) -> str:
    """POST /auth/password/forgot and return the mailed reset token."""
    r = client.post("/api/v1/auth/password/forgot", json={"email": email})
    assert r.status_code == 202, r.text
    return captured_emails.latest_token(email, "reset")


def reset_password(
    client: TestClient, token: str, new_password: str = NEW_PASSWORD
) -> Response:
    """POST /auth/password/reset with a mailed token."""
    return client.post(
        "/api/v1/auth/password/reset",
        json={"token": token, "new_password": new_password},
    )


class Db:
    """Synchronous SQL for fixtures and assertions (the `db` fixture).

    It uses the session's sync psycopg 3 engine and never touches the app's
    event loop or its async engine.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def rows(self, sql: str, **params: object) -> Sequence[RowMapping]:
        """All rows of `sql` as mappings."""
        with self._engine.connect() as conn:
            return conn.execute(text(sql), params).mappings().all()

    def one(self, sql: str, **params: object) -> RowMapping:
        """The single row of `sql`; fails the test on any other count."""
        rows = self.rows(sql, **params)
        assert len(rows) == 1, f"expected 1 row, got {len(rows)}: {sql}"
        return rows[0]

    def scalar(self, sql: str, **params: object) -> Any:  # noqa: ANN401 - the column type depends on the query
        """The first column of the first row, or None."""
        with self._engine.connect() as conn:
            return conn.execute(text(sql), params).scalar()

    def execute(self, sql: str, **params: object) -> None:
        """Run `sql` in its own committed transaction."""
        with self._engine.begin() as conn:
            conn.execute(text(sql), params)


def user_id_of(db: Db, username: str) -> str:
    """The id of the user `username`, as a string."""
    return str(db.scalar("SELECT id FROM users WHERE username = :u", u=username))


def active_session_count(db: Db, username: str) -> int:
    """Number of sessions of `username` that are not revoked."""
    return int(
        db.scalar(
            "SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE u.username = :u AND NOT s.is_revoked",
            u=username,
        )
    )


def audit_count(db: Db, event: str, username: str | None = None) -> int:
    """Number of audit rows of `event` (lower case), optionally of one user."""
    sql = "SELECT count(*) FROM auth_audit_log a WHERE lower(a.event_type::text) = :e"
    params: dict[str, object] = {"e": event}
    if username:
        sql += " AND a.user_id = (SELECT id FROM users WHERE username = :u)"
        params["u"] = username
    return int(db.scalar(sql, **params))


# The subject tells the mails apart (`app.modules.auth.tasks`).
_KIND_BY_SUBJECT = {
    "Verify your email address": "verify",
    "Reset your password": "reset",
    "Confirm your new email address": "change_email",
    "Your account has been locked": "account_locked",
    "A change of your email address was requested": "email_change_requested",
    "Your email address was changed": "email_change_completed",
}
_LINK = re.compile(r'href="([^"]+)"')


@dataclass(frozen=True)
class CapturedEmail:
    """One mail handed to the mailer, as the tests read it."""

    to: str
    # A token mail: verify, reset, change_email. A notice: account_locked,
    # email_change_requested, email_change_completed.
    kind: str
    token: str | None  # read from the link in the body; None in a notice
    username: str
    link: str
    html: str

    @classmethod
    def from_message(cls, message: MessageSchema) -> CapturedEmail:
        """Read recipient, kind, link and token from a rendered message."""
        [recipient] = message.recipients
        body = message.body  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] - fastapi-mail types the body as `str | list`
        assert isinstance(body, str)
        match = _LINK.search(body)
        assert match, "the mail has no link"
        link = html.unescape(match.group(1))
        tokens = parse_qs(urlsplit(link).query).get("token")
        return cls(
            to=str(recipient.email),
            kind=_KIND_BY_SUBJECT[message.subject],
            token=tokens[0] if tokens else None,
            username=recipient.name or "",
            link=link,
            html=body,
        )


class CapturedEmails(list[CapturedEmail]):
    """Every dispatched mail, in order, so tests can assert "no mail" too."""

    def to(self, address: str) -> list[CapturedEmail]:
        """The mails to `address`, case-insensitive."""
        return [m for m in self if m.to.lower() == address.lower()]

    def of_kind(self, kind: str) -> list[CapturedEmail]:
        """The mails of one kind."""
        return [m for m in self if m.kind == kind]

    def latest_token(self, address: str, kind: str | None = None) -> str:
        """The token of the latest token mail to `address`; notices are skipped."""
        tokens = [
            m.token
            for m in self.to(address)
            if m.token is not None and (kind is None or m.kind == kind)
        ]
        if not tokens:
            raise KeyError(address)
        return tokens[-1]


class CapturingMailer:
    """Stands in for fastapi-mail's `FastMail`; records instead of sending."""

    def __init__(self, emails: CapturedEmails) -> None:
        self.emails = emails

    async def send_message(self, message: MessageSchema) -> None:
        """Record `message`."""
        self.emails.append(CapturedEmail.from_message(message))


SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def admin_login(client: TestClient) -> None:
    """Log in to the admin panel; the client sends `Sec-Fetch-Site` from now on.

    The panel refuses state changes unless the browser marks them
    same-origin, as it does on the panel's own pages.
    """
    settings = get_settings()
    client.headers.update(SAME_ORIGIN)

    r = client.post(
        "/admin/login",
        data={
            "username": settings.ADMIN_USERNAME,
            "password": settings.ADMIN_PASSWORD.get_secret_value(),
        },
        follow_redirects=False,
    )
    assert r.status_code == 302, f"admin login failed: {r.status_code} {r.text[:200]}"


def run_action(
    client: TestClient,
    identity: str,
    slug: str,
    pks: list[str],
    *,
    headers: dict[str, str] | None = None,
) -> Response:
    """GET /admin/<identity>/action/<slug> for the rows `pks`, as the browser does."""
    return client.get(
        f"/admin/{identity}/action/{slug}",
        params={"pks": ",".join(pks)},
        headers=headers,
        follow_redirects=False,
    )


def redis_from_env(variable: str) -> redis_sync.Redis:
    """A sync client for the Redis URL in the environment `variable`.

    For example `TASKIQ_BROKER_URL`, the database that holds the task stream.
    """
    url = os.environ[variable]
    return redis_sync.Redis.from_url(url)  # pyright: ignore[reportUnknownMemberType] - redis-py leaves `**kwargs` of from_url() unannotated


def redis_keys(r: redis_sync.Redis, pattern: str) -> list[bytes]:
    """The keys matching `pattern` (KEYS is fine on a test database)."""
    return cast("list[bytes]", r.keys(pattern))  # pyright: ignore[reportUnknownMemberType] - redis-py leaves `**kwargs` of keys() unannotated


def redis_dump() -> bytes:
    """Every key and value in every Redis database the app uses."""
    port = int(os.environ["CACHE_PORT"])
    # redis-py types replies as `bytes | str` (it depends on decode_responses).
    chunks: list[bytes | str] = []
    for db_index in range(16):
        with redis_sync.Redis(port=port, db=db_index) as r:
            keys = cast("Iterator[bytes]", r.scan_iter())  # pyright: ignore[reportUnknownMemberType] - redis-py leaves `**kwargs` of scan_iter() unannotated
            for key in keys:
                chunks.append(key)
                kind = r.type(key)
                if kind == b"string":
                    chunks.append(r.get(key) or b"")
                elif kind == b"list":
                    chunks.extend(r.lrange(key, 0, -1))
                elif kind == b"stream":
                    for _id, fields in r.xrange(key) or []:
                        chunks.extend((fields or {}).values())
                elif kind == b"hash":
                    chunks.extend(r.hgetall(key).values())
                elif kind == b"set":
                    chunks.extend(r.smembers(key))
                elif kind == b"zset":
                    chunks.extend(cast("list[bytes]", r.zrange(key, 0, -1)))  # pyright: ignore[reportUnknownMemberType] - redis-py leaves `score_cast_func` of zrange() untyped
                else:
                    raise AssertionError(f"unexpected Redis type {kind!r}")
    return b"\n".join(c if isinstance(c, bytes) else c.encode() for c in chunks)


def redis_from_cache_env() -> redis_sync.Redis:
    """A sync client for the cache database of the test environment."""
    port = int(os.environ["CACHE_PORT"])
    return redis_sync.Redis(port=port, db=int(os.environ.get("CACHE_DB", "0")))


def set_cache_redis_config(name: str, value: int | str) -> None:
    """`CONFIG SET` on the cache's Redis (the session container)."""
    with redis_from_cache_env() as r:
        r.config_set(name, value)  # pyright: ignore[reportUnknownMemberType] - redis-py leaves `**kwargs` of config_set() unannotated


def replace_with_string(key: bytes) -> None:
    """Overwrite `key` in the cache database with a plain string value."""
    with redis_from_cache_env() as r:
        r.set(key, b"not a sorted set")


def rate_limit_keys() -> list[bytes]:
    """The rate-limit keys in the cache database (`rate_limit:*`)."""
    with redis_from_cache_env() as r:
        return redis_keys(r, "rate_limit:*")


def behind_proxy(app: ASGIApp) -> ASGIApp:
    """`app` behind uvicorn's proxy-header middleware, as `run.py` serves it.

    The test client is the trusted proxy, so each request chooses its client
    address with `forwarded_for(ip)`; without the header it is `testclient`.
    """
    # uvicorn types its middleware with its own ASGI protocol classes; at
    # run time both sides are plain ASGI callables.
    return cast("ASGIApp", ProxyHeadersMiddleware(cast("Any", app), trusted_hosts="*"))


def forwarded_for(ip: str) -> dict[str, str]:
    """The header that makes `behind_proxy` use `ip` as the client address."""
    return {"X-Forwarded-For": ip}


def assert_rate_limited(r: Response, *, window: int) -> None:
    """`r` is a 429 problem `rate_limited` with a `Retry-After` within `window`."""
    assert r.status_code == 429, f"{r.status_code} {r.text[:200]}"
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["code"] == "rate_limited"
    retry_after = r.headers["retry-after"]
    assert retry_after.isdigit(), retry_after
    assert 1 <= int(retry_after) <= window, retry_after


def assert_nothing_personal_in_redis(*secrets: str) -> None:
    """No PII marker and none of `secrets` is stored in any Redis database."""
    dump = redis_dump()
    for secret in (PII_USERNAME, PII_EMAIL, *secrets):
        assert secret.encode() not in dump, secret
        assert secret.lower().encode() not in dump.lower(), secret


def run_parallel[T](n: int, fn: Callable[[int], T]) -> list[T]:
    """Run `fn(i)` in n threads released simultaneously by a barrier.

    With one shared `TestClient` (entered as a context manager) all requests are
    scheduled on the same portal event loop, so they interleave at every await,
    which is exactly the interleaving a real server produces.
    """
    barrier = threading.Barrier(n)

    def _task(i: int) -> T:
        barrier.wait(timeout=30)
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_task, i) for i in range(n)]
        return [f.result(timeout=120) for f in futures]


def jwt_secret() -> str:
    """The signing key the app under test uses."""
    return get_settings().JWT_SECRET_KEY.get_secret_value()


def forge_access_token(
    user_id: str,
    *,
    secret: str,
    drop: tuple[str, ...] = (),
    algorithm: str = "HS256",
    **overrides: object,
) -> str:
    """Build a signed access token; `drop` removes claims, `overrides` replaces them."""
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "sub": user_id,
        "username": "forged",
        "jti": str(uuid.uuid4()),
        "type": "access",
        "exp": now + timedelta(minutes=5),
        "iat": now,
        "nbf": now,
    }
    claims.update(overrides)
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, secret, algorithm=algorithm)


def new_idempotency_key() -> str:
    """A fresh key as the frontend makes one: a UUIDv7 string."""
    return str(uuid.uuid7())


def keyed(key: str, headers: dict[str, str] | None = None) -> dict[str, str]:
    """`headers` plus `Idempotency-Key: <key>`."""
    return {**(headers or {}), "Idempotency-Key": key}


def idempotency_rows(db: Db, key: str | None = None) -> Sequence[RowMapping]:
    """The `idempotency_keys` rows, all or those of one key, oldest first."""
    if key is None:
        return db.rows("SELECT * FROM idempotency_keys ORDER BY created_at, id")
    return db.rows(
        "SELECT * FROM idempotency_keys WHERE key = :k ORDER BY created_at, id", k=key
    )


def assert_replayed(first: Response, retry: Response) -> None:
    """`retry` repeats `first` byte for byte, marked as a replay."""
    assert retry.status_code == first.status_code, retry.text
    assert retry.content == first.content
    assert retry.headers.get("content-type") == first.headers.get("content-type")
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert retry.headers["X-Request-ID"] != first.headers["X-Request-ID"]
    assert "set-cookie" not in retry.headers
