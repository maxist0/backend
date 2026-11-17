"""Plain helper functions shared by the test modules.

`BACKEND_DIR` serves both suites. Everything else (HTTP flows, SQL assertions,
parallel runners) is used by the integration tests.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import uuid
from collections.abc import Awaitable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import jwt
from fastapi.testclient import TestClient

from app.core.config import Settings

BACKEND_DIR = Path(__file__).resolve().parents[1]

PASSWORD = "Password123!"

# The lockout defaults the suite runs with (Settings defaults; no test
# environment overrides them).
MAXIMUM_LOGIN_ATTEMPTS: int = Settings.model_fields["LOGIN_MAX_ATTEMPTS"].default
LOGIN_LOCKOUT_SECONDS: int = Settings.model_fields["LOGIN_LOCKOUT_SECONDS"].default

# psycopg's async mode cannot run on Windows' default ProactorEventLoop (the app
# refuses to start on it, see app.core.database.ensure_supported_event_loop).
# Event loops are therefore created through this factory instead of the
# deprecated `WindowsSelectorEventLoopPolicy`; `None` means the platform
# default (uvloop-compatible asyncio default on POSIX).
LOOP_FACTORY: Callable[[], asyncio.AbstractEventLoop] | None = (
    asyncio.SelectorEventLoop if sys.platform == "win32" else None
)


def new_test_client(app: Any, **kwargs: Any) -> TestClient:
    """`TestClient` whose anyio portal runs on `LOOP_FACTORY`.

    Starlette passes `backend_options` to `anyio.from_thread.start_blocking_portal`,
    and anyio's asyncio backend forwards `loop_factory` to `asyncio.Runner`.
    """
    return TestClient(app, backend_options={"loop_factory": LOOP_FACTORY}, **kwargs)


def run_async[T](coro: Awaitable[T]) -> T:
    """Run `coro` to completion on a fresh loop made by `LOOP_FACTORY`.

    For tests that call async code outside the TestClient (own engines only,
    never the app engine, which lives on the TestClient's loop).
    """

    async def _wrapper() -> T:
        return await coro

    return asyncio.run(_wrapper(), loop_factory=LOOP_FACTORY)


def secret_value(value: Any) -> str:
    """Return the plain string of a setting that may or may not be a SecretStr."""
    return value.get_secret_value() if hasattr(value, "get_secret_value") else value


def register(
    client, *, username: str, email: str | None = None, password: str = PASSWORD
):
    return client.post(
        "/api/v1/auth/register",
        json={
            "username": username,
            "email": email or f"{username}@example.com",
            "password": password,
        },
    )


def register_ok(client, *, username: str, **kwargs):
    """register() that fails loudly unless the API answered 201.

    Callers must use the `captured_emails` fixture: without it the email task
    is enqueued on the real broker, and the test cannot read the token.
    """
    r = register(client, username=username, **kwargs)
    assert r.status_code == 201, f"register failed: {r.status_code} {r.text[:200]}"
    return r


def login(
    client, *, username: str, password: str = PASSWORD, headers: dict | None = None
):
    return client.post(
        "/api/v1/auth/login",
        data={"username": username, "password": password},
        headers=headers or {},
    )


def bearer(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


@dataclass
class Tokens:
    access: str
    refresh: str


def login_tokens(client, username: str, password: str = PASSWORD) -> Tokens:
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


def refresh_with(client, refresh_token: str):
    """POST /auth/refresh with an explicit refresh cookie.

    Returns (response, new_tokens_or_None). The jar is cleared afterwards.
    """
    r = client.post(
        "/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={refresh_token}"}
    )
    new_refresh = client.cookies.get("refresh_token")
    client.cookies.clear()
    if r.status_code != 200:
        return r, None
    return r, Tokens(access=r.json()["access_token"], refresh=new_refresh)


def user_id_of(db, username: str) -> str:
    return str(db.scalar("SELECT id FROM users WHERE username = :u", u=username))


def active_session_count(db, username: str) -> int:
    return db.scalar(
        "SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE u.username = :u AND NOT s.is_revoked",
        u=username,
    )


def audit_count(db, event: str, username: str | None = None) -> int:
    sql = "SELECT count(*) FROM auth_audit_log a WHERE lower(a.event_type::text) = :e"
    params: dict[str, Any] = {"e": event}
    if username:
        sql += " AND a.user_id = (SELECT id FROM users WHERE username = :u)"
        params["u"] = username
    return db.scalar(sql, **params)


def run_parallel(n: int, fn: Callable[[int], Any]) -> list[Any]:
    """Run `fn(i)` in n threads released simultaneously by a barrier.

    With one shared `TestClient` (entered as a context manager) all requests are
    scheduled on the same portal event loop, so they interleave at every await,
    which is exactly the interleaving a real server produces.
    """
    barrier = threading.Barrier(n)

    def _task(i: int) -> Any:
        barrier.wait(timeout=30)
        return fn(i)

    with ThreadPoolExecutor(max_workers=n) as pool:
        futures = [pool.submit(_task, i) for i in range(n)]
        return [f.result(timeout=120) for f in futures]


def forge_access_token(
    user_id: str,
    *,
    secret: str,
    drop: tuple[str, ...] = (),
    algorithm: str = "HS256",
    **overrides: Any,
) -> str:
    """Build a signed access token; `drop` removes claims, `overrides` replaces them."""
    now = datetime.now(timezone.utc)
    claims: dict[str, Any] = {
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
