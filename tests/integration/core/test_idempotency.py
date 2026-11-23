"""`Idempotency-Key` on the Postgres container, through routes a module would write.

The probe routes use only the public interface: `Idempotent` from
`app.modules.users` and `DbSession`, `Conflict`, `RateLimitedError` from
`app.core`. They are anonymous, so their keys are scoped by route. Faults
(a crash after the commit, a rollback on the success path, a parallel
request) are written into the probe routes; the built-in routes are covered
in `tests/integration/modules/auth/test_idempotent_routes.py`.

`TestPostgresBehaviour` proves the two PostgreSQL facts the design relies
on, on the pinned PostgreSQL image, without the app.
"""

import asyncio
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
import time_machine
from fastapi import APIRouter, FastAPI, Request, Response, status
from fastapi.testclient import TestClient
from httpx2 import Response as HttpResponse
from prometheus_client import REGISTRY
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError

from app.core import Conflict, DbSession, RateLimitedError
from app.core import idempotency as idempotency_module
from app.core.config import get_settings
from app.core.idempotency import (
    IdempotencyKey,
    delete_expired_keys,
    idempotency_cleanup_task,
)
from app.modules.users import CurrentUser, Idempotent
from tests.helpers import (
    NEW_PASSWORD,
    PASSWORD,
    Db,
    app_of,
    assert_replayed,
    bearer,
    call_in_app,
    idempotency_rows,
    keyed,
    log_events,
    login_tokens,
    new_idempotency_key,
    new_test_client,
    register_and_login,
    register_ok,
    run_parallel,
    run_task,
)

BASE = "/api/v1/probe-idempotency"
ME = "/api/v1/users/me"

probe = APIRouter(prefix="/probe-idempotency", tags=["probe"])


class ProbeRequest(BaseModel):
    """Any body; the fingerprint covers it."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(max_length=64)


class ProbeResponse(BaseModel):
    """Which execution answered."""

    value: str
    call: int


def _count(request: Request) -> int:
    request.app.state.probe_calls += 1
    calls: int = request.app.state.probe_calls
    return calls


async def _wait_for_gate(request: Request) -> None:
    gate: threading.Event | None = request.app.state.probe_gate
    if gate is not None:
        request.app.state.probe_reached.set()
        released = await asyncio.get_running_loop().run_in_executor(None, gate.wait, 30)
        assert released, "gate was never released"


@probe.post(
    "/echo",
    status_code=status.HTTP_201_CREATED,
    summary="Writes nothing",
    dependencies=[Idempotent],
)
async def echo(body: ProbeRequest, request: Request) -> ProbeResponse:
    """No write: the guard commits the key row together with the response."""
    return ProbeResponse(value=body.value, call=_count(request))


@probe.post("/gated", summary="Pauses before any commit", dependencies=[Idempotent])
async def gated(request: Request) -> ProbeResponse:
    """The key row stays uncommitted while the gate is closed."""
    call = _count(request)
    await _wait_for_gate(request)
    return ProbeResponse(value="gated", call=call)


@probe.post(
    "/gated-after-commit",
    summary="Commits, then pauses",
    dependencies=[Idempotent],
)
async def gated_after_commit(request: Request, db: DbSession) -> ProbeResponse:
    """An early commit (like a password confirmation) commits the key row."""
    call = _count(request)
    await db.commit()
    await _wait_for_gate(request)
    return ProbeResponse(value="gated", call=call)


@probe.post(
    "/commit-then-fail", summary="Crash after the commit", dependencies=[Idempotent]
)
async def commit_then_fail(request: Request, db: DbSession) -> None:
    """The business committed; the response is lost."""
    _count(request)
    await db.commit()
    raise RuntimeError("process died")


@probe.post(
    "/commit-then-conflict",
    summary="Refusal after an early commit",
    dependencies=[Idempotent],
)
async def commit_then_conflict(request: Request, db: DbSession) -> None:
    """A refusal whose counters were committed: stored and replayed."""
    _count(request)
    await db.commit()
    raise Conflict("Probe conflict.")


@probe.post(
    "/fail-before-commit",
    summary="Refusal before any commit",
    dependencies=[Idempotent],
)
async def fail_before_commit(request: Request) -> None:
    """Rolled back: no row, a retry runs again."""
    _count(request)
    raise Conflict("Probe conflict.")


@probe.post(
    "/commit-then-limited",
    summary="Rate limit after an early commit",
    dependencies=[Idempotent],
)
async def commit_then_limited(request: Request, db: DbSession) -> None:
    """A 429 is never stored: the row goes, the retry runs again."""
    _count(request)
    await db.commit()
    raise RateLimitedError(retry_after=7)


@probe.post("/lock-timeout", summary="Reads lock_timeout", dependencies=[Idempotent])
async def lock_timeout(db: DbSession) -> dict[str, str]:
    """`lock_timeout` as the service sees it after the guard."""
    value = (await db.execute(text("SHOW lock_timeout"))).scalar_one()
    return {"lock_timeout": str(value)}


@probe.post("/set-cookie", summary="Sets a cookie", dependencies=[Idempotent])
async def set_cookie(request: Request, response: Response) -> ProbeResponse:
    """Never stored, never replayed."""
    response.set_cookie("probe", "secret")
    return ProbeResponse(value="cookie", call=_count(request))


@probe.post("/big-body", summary="Large response body", dependencies=[Idempotent])
async def big_body(request: Request) -> dict[str, str]:
    """A body of about 70 KiB."""
    _count(request)
    return {"blob": "x" * 70_000}


@probe.post(
    "/rollback-success",
    summary="Rolls back, then succeeds",
    dependencies=[Idempotent],
)
async def rollback_success(request: Request, db: DbSession) -> ProbeResponse:
    """A service that ends its transaction with a rollback loses the key row."""
    await db.rollback()
    return ProbeResponse(value="rollback", call=_count(request))


@probe.post(
    "/uncommitted",
    summary="Leaves an ORM change pending",
    dependencies=[Idempotent],
)
async def uncommitted(request: Request, db: DbSession) -> ProbeResponse:
    """The guard must not commit what the route left pending."""
    db.add(IdempotencyKey(scope="probe", key="p" * 16, fingerprint=b"x"))
    return ProbeResponse(value="pending", call=_count(request))


@probe.post("/mine", summary="Marker before the user", dependencies=[Idempotent])
async def mine(current_user: CurrentUser) -> dict[str, str]:
    """The guard's scope runs before `CurrentUser` here (no `by_user` limit)."""
    return {"user": str(current_user.id)}


@pytest.fixture
def probe_app(app: FastAPI) -> FastAPI:
    """The test app with the probe router and its call counter."""
    app.include_router(probe, prefix=get_settings().API_PREFIX)
    app.state.probe_calls = 0
    app.state.probe_gate = None
    app.state.probe_reached = threading.Event()
    return app


@pytest.fixture
def probe_client(probe_app: FastAPI) -> Iterator[TestClient]:
    """A client of the probe app."""
    with new_test_client(probe_app) as client:
        yield client


@pytest.fixture
def gate(probe_app: FastAPI) -> Iterator[threading.Event]:
    """Close the gate of the gated probes; always reopened at the end."""
    release = threading.Event()
    probe_app.state.probe_gate = release
    yield release
    release.set()


def _calls(client: TestClient) -> int:
    calls: int = app_of(client).state.probe_calls
    return calls


def _echo(client: TestClient, key: str, value: str = "a") -> HttpResponse:
    return client.post(f"{BASE}/echo", json={"value": value}, headers=keyed(key))


def _outcome(outcome: str) -> float:
    labels = {"outcome": outcome}
    return REGISTRY.get_sample_value("idempotency_requests_total", labels) or 0.0


class TestPostgresBehaviour:
    """The PostgreSQL behaviour the guard depends on, checked on PG 18."""

    @staticmethod
    def _url(engine: Engine) -> str:
        return engine.url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )

    def _insert(self, conn: psycopg.Connection[Any], key: str) -> list[Any]:
        return conn.execute(
            "INSERT INTO idempotency_keys (id, scope, key, fingerprint) "
            "VALUES (gen_random_uuid(), 'route:probe', %s, '\\x00') "
            "ON CONFLICT (scope, key) DO NOTHING RETURNING id",
            (key,),
        ).fetchall()

    def test_a_conflicting_insert_waits_and_lock_timeout_ends_the_wait(
        self, sync_engine: Engine
    ) -> None:
        url = self._url(sync_engine)
        key = new_idempotency_key()
        with psycopg.connect(url) as first, psycopg.connect(url) as second:
            assert self._insert(first, key)  # uncommitted
            second.execute("SET LOCAL lock_timeout = '300ms'")
            started = time.monotonic()
            with pytest.raises(psycopg.errors.LockNotAvailable) as raised:
                self._insert(second, key)
            assert time.monotonic() - started >= 0.25
            assert raised.value.sqlstate == "55P03"

    @pytest.mark.parametrize(("finish", "inserted"), [("commit", 0), ("rollback", 1)])
    def test_the_waiting_insert_follows_the_first_transaction(
        self, sync_engine: Engine, finish: str, inserted: int
    ) -> None:
        url = self._url(sync_engine)
        key = new_idempotency_key()
        with psycopg.connect(url) as first, psycopg.connect(url) as second:
            assert self._insert(first, key)
            timer = threading.Timer(0.2, getattr(first, finish))
            timer.start()
            second.execute("SET LOCAL lock_timeout = '5s'")
            rows = self._insert(second, key)
            timer.join()
            second.commit()
        assert len(rows) == inserted

    def test_set_local_to_default_restores_the_session_value(
        self, sync_engine: Engine
    ) -> None:
        url = self._url(sync_engine)
        with psycopg.connect(url, options="-c lock_timeout=7s") as conn:
            conn.execute("SET LOCAL lock_timeout = '500ms'")
            conn.execute("SET LOCAL lock_timeout TO DEFAULT")
            assert conn.execute("SHOW lock_timeout").fetchone() == ("7s",)
        with psycopg.connect(url) as conn:
            conn.execute("SET LOCAL lock_timeout = '500ms'")
            conn.execute("SET LOCAL lock_timeout TO DEFAULT")
            assert conn.execute("SHOW lock_timeout").fetchone() == ("0",)


class TestWithoutKey:
    def test_a_request_without_key_runs_every_time_and_stores_nothing(
        self, probe_client: TestClient, db: Db
    ) -> None:
        for _ in range(2):
            r = probe_client.post(f"{BASE}/echo", json={"value": "a"})
            assert r.status_code == 201
            assert "Idempotent-Replayed" not in r.headers

        assert _calls(probe_client) == 2
        assert idempotency_rows(db) == []

    def test_the_old_frontend_header_is_ignored(
        self, probe_client: TestClient, db: Db
    ) -> None:
        key = new_idempotency_key()
        for _ in range(2):
            r = probe_client.post(
                f"{BASE}/echo", json={"value": "a"}, headers={"X-Idempotency-Key": key}
            )
            assert r.status_code == 201

        assert _calls(probe_client) == 2
        assert idempotency_rows(db) == []


class TestHeader:
    @pytest.mark.parametrize(
        "value",
        ["", "short-key", "comma,separated,key-0001", "ümlaut-key-000000001"],
    )
    def test_a_malformed_key_gets_400_and_nothing_runs(
        self, probe_client: TestClient, db: Db, value: str
    ) -> None:
        r = probe_client.post(
            f"{BASE}/echo",
            json={"value": "a"},
            headers={b"Idempotency-Key": value.encode()},
        )

        assert r.status_code == 400, r.text
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["code"] == "idempotency_key_invalid"
        assert _calls(probe_client) == 0
        assert idempotency_rows(db) == []

    def test_two_key_headers_get_400(self, probe_client: TestClient) -> None:
        key = new_idempotency_key()
        r = probe_client.post(
            f"{BASE}/echo",
            json={"value": "a"},
            headers=[("Idempotency-Key", key), ("Idempotency-Key", key)],
        )

        assert r.status_code == 400
        assert r.json()["code"] == "idempotency_key_invalid"

    def test_quoted_and_bare_keys_are_the_same_key(
        self, probe_client: TestClient, db: Db
    ) -> None:
        key = new_idempotency_key()
        first = _echo(probe_client, key)
        retry = probe_client.post(
            f"{BASE}/echo", json={"value": "a"}, headers={"Idempotency-Key": f'"{key}"'}
        )

        assert first.status_code == 201
        assert_replayed(first, retry)
        assert [row["key"] for row in idempotency_rows(db)] == [key]


class TestReplay:
    def test_a_retry_gets_the_stored_response_and_runs_nothing(
        self, probe_client: TestClient, db: Db
    ) -> None:
        key = new_idempotency_key()
        replays_before = _outcome("replayed")

        first = _echo(probe_client, key)
        retry = _echo(probe_client, key)

        assert first.status_code == 201
        assert first.json() == {"value": "a", "call": 1}
        assert_replayed(first, retry)
        assert _calls(probe_client) == 1
        assert _outcome("replayed") == replays_before + 1
        (row,) = idempotency_rows(db, key)
        assert row["scope"] == f"route:POST {BASE}/echo"
        assert row["response_status"] == 201
        assert row["response_headers"] == [["content-type", "application/json"]]
        assert bytes(row["response_body"]) == first.content
        assert len(bytes(row["fingerprint"])) == 32

    def test_a_replay_keeps_the_cors_exposure(self, probe_client: TestClient) -> None:
        key = new_idempotency_key()
        origin = {"Origin": "http://localhost:5173"}
        _echo(probe_client, key)

        retry = probe_client.post(
            f"{BASE}/echo", json={"value": "a"}, headers=keyed(key, origin)
        )

        assert retry.headers["Idempotent-Replayed"] == "true"
        exposed = retry.headers["access-control-expose-headers"]
        assert "Idempotent-Replayed" in exposed

    def test_a_stored_refusal_is_replayed_and_not_repeated(
        self, probe_client: TestClient
    ) -> None:
        key = new_idempotency_key()

        first = probe_client.post(f"{BASE}/commit-then-conflict", headers=keyed(key))
        retry = probe_client.post(f"{BASE}/commit-then-conflict", headers=keyed(key))

        assert first.status_code == 409
        assert first.json()["code"] == "conflict"
        assert_replayed(first, retry)
        assert _calls(probe_client) == 1


class TestMismatch:
    def test_the_same_key_with_another_body_gets_422(
        self, probe_client: TestClient
    ) -> None:
        key = new_idempotency_key()
        _echo(probe_client, key, "a")

        r = _echo(probe_client, key, "b")

        assert r.status_code == 422
        assert r.headers["content-type"] == "application/problem+json"
        assert r.json()["code"] == "idempotency_key_reused"
        assert "errors" not in r.json()
        assert _calls(probe_client) == 1

    def test_the_same_key_with_another_query_gets_422(
        self, probe_client: TestClient
    ) -> None:
        key = new_idempotency_key()
        _echo(probe_client, key)

        r = probe_client.post(
            f"{BASE}/echo?x=1", json={"value": "a"}, headers=keyed(key)
        )

        assert r.status_code == 422
        assert r.json()["code"] == "idempotency_key_reused"

    def test_another_key_with_the_same_body_runs_again(
        self, probe_client: TestClient
    ) -> None:
        _echo(probe_client, new_idempotency_key())
        r = _echo(probe_client, new_idempotency_key())

        assert r.json()["call"] == 2


class TestErrorsBeforeTheCommit:
    def test_a_refusal_before_any_commit_leaves_no_row(
        self, probe_client: TestClient, db: Db
    ) -> None:
        key = new_idempotency_key()

        first = probe_client.post(f"{BASE}/fail-before-commit", headers=keyed(key))
        retry = probe_client.post(f"{BASE}/fail-before-commit", headers=keyed(key))

        assert first.status_code == retry.status_code == 409
        assert "Idempotent-Replayed" not in retry.headers
        assert _calls(probe_client) == 2
        assert idempotency_rows(db) == []

    def test_a_validation_error_leaves_no_row(
        self, probe_client: TestClient, db: Db
    ) -> None:
        key = new_idempotency_key()

        r = probe_client.post(f"{BASE}/echo", json={"value": 1}, headers=keyed(key))

        assert r.status_code == 422
        assert r.json()["code"] == "validation_error"
        assert idempotency_rows(db) == []

    def test_a_429_after_a_commit_deletes_the_row_and_the_retry_runs(
        self, probe_client: TestClient, db: Db
    ) -> None:
        key = new_idempotency_key()

        first = probe_client.post(f"{BASE}/commit-then-limited", headers=keyed(key))
        assert idempotency_rows(db) == []
        retry = probe_client.post(f"{BASE}/commit-then-limited", headers=keyed(key))

        assert first.status_code == retry.status_code == 429
        assert retry.headers["retry-after"] == "7"
        assert "Idempotent-Replayed" not in retry.headers
        assert _calls(probe_client) == 2


class TestCrashWindow:
    def test_a_lost_result_is_never_run_again(
        self, probe_client: TestClient, db: Db
    ) -> None:
        key = new_idempotency_key()

        crashed = probe_client.post(f"{BASE}/commit-then-fail", headers=keyed(key))
        in_flight = probe_client.post(f"{BASE}/commit-then-fail", headers=keyed(key))
        db.execute(
            "UPDATE idempotency_keys "
            "SET created_at = created_at - interval '61 seconds'"
        )
        unknown = probe_client.post(f"{BASE}/commit-then-fail", headers=keyed(key))

        assert crashed.status_code == 500
        assert in_flight.status_code == 409
        assert in_flight.json()["code"] == "idempotency_key_in_use"
        assert in_flight.headers["retry-after"] == "1"
        assert unknown.status_code == 409
        assert unknown.json()["code"] == "idempotency_outcome_unknown"
        assert "retry-after" not in unknown.headers
        assert _calls(probe_client) == 1
        (row,) = idempotency_rows(db, key)
        assert row["response_status"] is None

    @pytest.mark.parametrize(
        ("route", "status"),
        [("gated-after-commit", 200), ("commit-then-conflict", 409)],
    )
    def test_a_failed_store_leaves_the_row_without_response(
        self,
        probe_client: TestClient,
        db: Db,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        route: str,
        status: int,
    ) -> None:
        """The client still gets its answer; a retry never runs again."""

        async def broken_store(*_args: object, **_kwargs: object) -> bool:
            raise OperationalError("UPDATE", {}, Exception("connection lost"))

        key = new_idempotency_key()
        not_stored_before = _outcome("not_stored")
        monkeypatch.setattr(idempotency_module, "_store", broken_store)
        first = probe_client.post(f"{BASE}/{route}", headers=keyed(key))
        monkeypatch.undo()
        not_stored_after = _outcome("not_stored")
        retry = probe_client.post(f"{BASE}/{route}", headers=keyed(key))

        assert first.status_code == status
        assert retry.status_code == 409
        assert retry.json()["code"] == "idempotency_key_in_use"
        assert _calls(probe_client) == 1
        (row,) = idempotency_rows(db, key)
        assert row["response_status"] is None
        assert len(log_events(caplog, "idempotency.store_failed")) == 1
        assert not_stored_after == not_stored_before + 1


class TestNotStored:
    def test_a_response_with_set_cookie_is_never_stored(
        self, probe_client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        key = new_idempotency_key()

        first = probe_client.post(f"{BASE}/set-cookie", headers=keyed(key))
        retry = probe_client.post(f"{BASE}/set-cookie", headers=keyed(key))

        assert first.status_code == retry.status_code == 200
        assert "Idempotent-Replayed" not in retry.headers
        assert _calls(probe_client) == 2
        assert idempotency_rows(db) == []
        events = log_events(caplog, "idempotency.response_not_stored")
        assert [e["reason"] for e in events] == ["set_cookie", "set_cookie"]

    def test_a_response_above_the_cap_is_not_stored(
        self, probe_client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        key = new_idempotency_key()

        r = probe_client.post(f"{BASE}/big-body", headers=keyed(key))

        assert r.status_code == 200
        assert len(r.content) > 64 * 1024
        assert idempotency_rows(db) == []
        events = log_events(caplog, "idempotency.response_not_stored")
        assert [e["reason"] for e in events] == ["too_large"]

    def test_pending_orm_changes_are_not_committed_by_the_guard(
        self, probe_client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        key = new_idempotency_key()

        r = probe_client.post(f"{BASE}/uncommitted", headers=keyed(key))

        assert r.status_code == 200
        assert idempotency_rows(db) == []
        events = log_events(caplog, "idempotency.response_not_stored")
        assert [e["reason"] for e in events] == ["uncommitted_changes"]

    def test_a_rollback_on_the_success_path_is_reported(
        self, probe_client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        key = new_idempotency_key()

        r = probe_client.post(f"{BASE}/rollback-success", headers=keyed(key))

        assert r.status_code == 200
        assert idempotency_rows(db) == []
        assert len(log_events(caplog, "idempotency.key_row_lost")) == 1


class TestLockTimeout:
    def test_the_service_runs_with_the_server_lock_timeout(
        self, probe_client: TestClient
    ) -> None:
        r = probe_client.post(
            f"{BASE}/lock-timeout", headers=keyed(new_idempotency_key())
        )

        assert r.json() == {"lock_timeout": "0"}

    def test_a_business_lock_wait_longer_than_the_guard_timeout_succeeds(
        self, client: TestClient, sync_engine: Engine
    ) -> None:
        """A user row lock held for 1 s must not hit the guard's 500 ms."""
        access = register_and_login(client)
        with sync_engine.connect() as holder:
            holder.execute(
                text("SELECT id FROM users WHERE username = 'alice' FOR NO KEY UPDATE")
            )
            timer = threading.Timer(1.0, holder.commit)
            timer.start()
            started = time.monotonic()
            r = client.put(
                "/api/v1/users/me/password",
                json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
                headers=keyed(new_idempotency_key(), bearer(access)),
            )
            elapsed = time.monotonic() - started
            timer.join()

        assert r.status_code == 204, r.text
        assert elapsed >= 0.9


class TestParallel:
    def test_a_parallel_request_waits_then_gets_409(
        self, probe_client: TestClient, gate: threading.Event, db: Db
    ) -> None:
        key = new_idempotency_key()
        reached: threading.Event = app_of(probe_client).state.probe_reached
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(probe_client.post, f"{BASE}/gated", headers=keyed(key))
            assert reached.wait(30)
            started = time.monotonic()
            second = probe_client.post(f"{BASE}/gated", headers=keyed(key))
            waited = time.monotonic() - started
            gate.set()
            first_response = first.result(timeout=30)
        retry = probe_client.post(f"{BASE}/gated", headers=keyed(key))

        assert second.status_code == 409
        assert second.json()["code"] == "idempotency_key_in_use"
        assert second.headers["retry-after"] == "1"
        assert 0.45 <= waited < 5
        assert first_response.status_code == 200
        assert_replayed(first_response, retry)
        assert _calls(probe_client) == 1
        assert len(idempotency_rows(db, key)) == 1

    def test_after_an_early_commit_a_parallel_request_gets_409_at_once(
        self, probe_client: TestClient, gate: threading.Event
    ) -> None:
        key = new_idempotency_key()
        reached: threading.Event = app_of(probe_client).state.probe_reached
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(
                probe_client.post, f"{BASE}/gated-after-commit", headers=keyed(key)
            )
            assert reached.wait(30)
            started = time.monotonic()
            second = probe_client.post(f"{BASE}/gated-after-commit", headers=keyed(key))
            waited = time.monotonic() - started
            gate.set()
            assert first.result(timeout=30).status_code == 200

        assert second.status_code == 409
        assert second.json()["code"] == "idempotency_key_in_use"
        assert waited < 0.45
        assert _calls(probe_client) == 1

    @pytest.mark.parametrize("attempt", range(5))
    def test_a_burst_with_one_key_runs_once(
        self, probe_client: TestClient, db: Db, attempt: int
    ) -> None:
        key = new_idempotency_key()

        responses = run_parallel(8, lambda _i: _echo(probe_client, key))

        assert _calls(probe_client) == 1, attempt
        executed = [r for r in responses if r.status_code == 201]
        assert executed
        assert all(r.json() == {"value": "a", "call": 1} for r in executed)
        assert sum("Idempotent-Replayed" not in r.headers for r in executed) == 1
        assert {r.status_code for r in responses} <= {201, 409}
        assert all(
            r.json()["code"] == "idempotency_key_in_use"
            for r in responses
            if r.status_code == 409
        )
        assert len(idempotency_rows(db, key)) == 1


class TestScope:
    def test_users_do_not_share_keys(self, client: TestClient, db: Db) -> None:
        alice = register_and_login(client, username="alice")
        bob = register_and_login(client, username="bob")
        key = new_idempotency_key()
        body = {"display_name": "Same Name"}

        r_alice = client.patch(
            "/api/v1/users/me", json=body, headers=keyed(key, bearer(alice))
        )
        r_bob = client.patch(
            "/api/v1/users/me", json=body, headers=keyed(key, bearer(bob))
        )

        assert r_alice.status_code == r_bob.status_code == 200
        assert "Idempotent-Replayed" not in r_bob.headers
        assert r_bob.json()["username"] == "bob"
        scopes = sorted(row["scope"] for row in idempotency_rows(db, key))
        assert scopes == sorted(
            f"user:{db.scalar('SELECT id FROM users WHERE username = :u', u=name)}"
            for name in ("alice", "bob")
        )

    def test_one_user_on_two_routes_with_one_key_gets_422(
        self, client: TestClient
    ) -> None:
        access = register_and_login(client)
        key = new_idempotency_key()
        client.patch(
            "/api/v1/users/me",
            json={"display_name": "A"},
            headers=keyed(key, bearer(access)),
        )

        r = client.put(
            "/api/v1/users/me/password",
            json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
            headers=keyed(key, bearer(access)),
        )

        assert r.status_code == 422
        assert r.json()["code"] == "idempotency_key_reused"

    @pytest.mark.parametrize(
        "blocked",
        [
            "UPDATE users SET is_active = false",
            "UPDATE users SET is_locked = true, locked_until = NULL",
        ],
        ids=["deactivated", "admin-locked"],
    )
    def test_a_blocked_account_never_reaches_its_stored_responses(
        self, client: TestClient, db: Db, blocked: str
    ) -> None:
        access = register_and_login(client)
        key = new_idempotency_key()
        body = {"display_name": "A"}
        stored = client.patch(ME, json=body, headers=keyed(key, bearer(access)))
        assert stored.status_code == 200
        db.execute(blocked)

        r = client.patch(ME, json=body, headers=keyed(key, bearer(access)))

        assert r.status_code == 401
        assert "Idempotent-Replayed" not in r.headers

    @pytest.mark.parametrize("path", [ME, f"{BASE}/mine"])
    def test_an_outdated_token_never_reaches_the_users_responses(
        self,
        probe_client: TestClient,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        path: str,
    ) -> None:
        """A token issued before the last password change gets 401, once logged.

        On the probe route the guard's scope sees the token before
        `CurrentUser`; only `CurrentUser` logs the refusal.
        """
        register_ok(probe_client, username="alice")
        with time_machine.travel(datetime.now(UTC) - timedelta(seconds=10)):
            access = login_tokens(probe_client, "alice").access
        key = new_idempotency_key()
        body = {"display_name": "A"} if path == ME else None
        stored = probe_client.request(
            "PATCH" if path == ME else "POST",
            path,
            json=body,
            headers=keyed(key, bearer(access)),
        )
        assert stored.status_code == 200
        db.execute("UPDATE users SET password_changed_at = now()")
        caplog.clear()

        r = probe_client.request(
            "PATCH" if path == ME else "POST",
            path,
            json=body,
            headers=keyed(key, bearer(access)),
        )

        assert r.status_code == 401
        assert "Idempotent-Replayed" not in r.headers
        assert len(log_events(caplog, "auth.access_token_outdated")) == 1


class TestCleanup:
    def _insert(self, db: Db, key: str, age: timedelta) -> None:
        db.execute(
            "INSERT INTO idempotency_keys (id, scope, key, fingerprint, created_at) "
            "VALUES (gen_random_uuid(), 'route:probe', :k, '\\x00', now() - :age)",
            k=key,
            age=age,
        )

    def test_the_task_deletes_keys_past_the_retention_only(
        self, client: TestClient, db: Db, caplog: pytest.LogCaptureFixture
    ) -> None:
        old, young = new_idempotency_key(), new_idempotency_key()
        self._insert(db, old, timedelta(hours=24, minutes=1))
        self._insert(db, young, timedelta(hours=23, minutes=59))

        run_task(client, idempotency_cleanup_task)

        assert [row["key"] for row in idempotency_rows(db)] == [young]
        (event,) = log_events(caplog, "idempotency.cleanup_done")
        assert event["deleted"] == 1
        assert event["capped"] is False

    def test_a_run_stops_at_the_cap_and_reports_it(
        self,
        client: TestClient,
        db: Db,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for _ in range(5):
            self._insert(db, new_idempotency_key(), timedelta(days=2))
        monkeypatch.setattr(idempotency_module, "CLEANUP_BATCH_SIZE", 2)
        monkeypatch.setattr(idempotency_module, "CLEANUP_MAX_BATCHES", 2)
        capped_before = (
            REGISTRY.get_sample_value("idempotency_cleanup_capped_total") or 0.0
        )

        run_task(client, idempotency_cleanup_task)

        assert len(idempotency_rows(db)) == 1
        (event,) = log_events(caplog, "idempotency.cleanup_done")
        assert (event["deleted"], event["capped"], event["level"]) == (
            4,
            True,
            "warning",
        )
        capped_after = REGISTRY.get_sample_value("idempotency_cleanup_capped_total")
        assert capped_after == capped_before + 1

    def test_the_cleanup_runs_in_batches_and_reports_a_cap(
        self, client: TestClient, db: Db
    ) -> None:
        for _ in range(5):
            self._insert(db, new_idempotency_key(), timedelta(days=2))
        factory = app_of(client).state.session_factory

        async def _run(max_batches: int) -> tuple[int, bool]:
            async with factory() as session:
                return await delete_expired_keys(
                    session, timedelta(hours=24), batch_size=2, max_batches=max_batches
                )

        assert call_in_app(client, lambda: _run(2)) == (4, True)
        assert call_in_app(client, lambda: _run(2)) == (1, False)
        assert idempotency_rows(db) == []
