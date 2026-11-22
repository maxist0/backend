"""Taskiq on a real Redis: delivery, acks that delete, retries, results, API broker."""

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from fastapi.testclient import TestClient
from taskiq import AckableMessage, AsyncResultBackend
from taskiq.api import run_receiver_task
from taskiq.message import BrokerMessage, TaskiqMessage
from taskiq.result import TaskiqResult

import app.core.tasks as app_tasks
import app.modules  # noqa: F401  # pyright: ignore[reportUnusedImport] - registers the module tasks
from app.core.config import get_settings
from app.core.mail import MailDeliveryError, TransientMailDeliveryError
from app.core.tasks import (
    RETRY_SCHEDULE_PREFIX,
    TASK_RESULT_TTL_SECONDS,
    TASK_STREAM,
    build_broker,
    build_retry_middleware,
    build_retry_source,
    taskiq_heartbeat,
)
from app.modules.auth.tasks import send_token_email_task
from tests.helpers import (
    app_of,
    redis_from_env,
    redis_keys,
    run_async,
)


def _probe(n: int) -> BrokerMessage:
    return BrokerMessage(
        task_id=f"t-{n}", task_name="probe", message=f"payload-{n}".encode(), labels={}
    )


async def _next_message(
    listener: AsyncGenerator[bytes | AckableMessage],
) -> AckableMessage:
    """The next entry the stream broker delivers; it always delivers ackables."""
    message = await anext(listener)
    assert isinstance(message, AckableMessage)
    return message


async def _ack(message: AckableMessage) -> None:
    acked = message.ack()
    assert acked is not None, "the stream broker acknowledges asynchronously"
    await acked


def _stream_state() -> tuple[int, int]:
    """Length of the task stream and number of pending entries."""
    with redis_from_env("TASKIQ_BROKER_URL") as r:
        return r.xlen(TASK_STREAM), r.xpending(TASK_STREAM, "taskiq")["pending"]


def test_stream_message_is_pending_until_acknowledged_then_deleted() -> None:
    """At least once: unacknowledged entries stay; the ack deletes the entry.

    So a processed task's arguments do not linger in Redis.
    """

    async def scenario() -> tuple[bytes, tuple[int, int], tuple[int, int]]:
        broker = build_broker(get_settings())
        await broker.startup()
        try:
            await broker.kick(_probe(1))
            listener = broker.listen()
            message = await _next_message(listener)
            await listener.aclose()
            before = _stream_state()
            await _ack(message)
            return message.data, before, _stream_state()
        finally:
            await broker.shutdown()

    data, before, after = run_async(scenario())

    assert data == b"payload-1"
    assert before == (1, 1)
    assert after == (0, 0)


def test_worker_deletes_the_entry_of_a_finished_task() -> None:
    """A real receiver runs the task, stores the result, acks: XLEN is 0."""

    async def scenario() -> tuple[int, int]:
        broker = build_broker(get_settings())
        await broker.startup()
        worker = asyncio.create_task(run_receiver_task(broker))
        try:
            await taskiq_heartbeat.kicker().with_broker(broker).kiq()
            async with asyncio.timeout(10):
                while _stream_state() != (0, 0):  # noqa: ASYNC110 - polls Redis; there is no event to wait on
                    await asyncio.sleep(0.05)
            return _stream_state()
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            await broker.shutdown()

    assert run_async(scenario()) == (0, 0)


def test_reclaimed_entry_is_deleted_on_ack_too() -> None:
    """A reclaimed entry's ack deletes it like the ack of a newly read entry.

    An entry left unacknowledged is claimed again (XAUTOCLAIM).
    """

    async def scenario() -> tuple[bytes, tuple[int, int]]:
        broker = build_broker(get_settings())
        assert isinstance(broker, app_tasks.ReconnectingStreamBroker)
        # Claim at once instead of after the production idle timeout.
        broker.idle_timeout = 0
        await broker.startup()
        try:
            await broker.kick(_probe(2))
            listener = broker.listen()
            await anext(listener)  # delivered, never acknowledged
            reclaimed = await _next_message(listener)
            await listener.aclose()
            await _ack(reclaimed)
            return reclaimed.data, _stream_state()
        finally:
            await broker.shutdown()

    data, after = run_async(scenario())

    assert data == b"payload-2"
    assert after == (0, 0)


def test_listen_recovers_when_the_group_disappears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FLUSHALL keeps the connection but drops the consumer group (NOGROUP)."""
    monkeypatch.setattr(app_tasks, "BROKER_RECONNECT_SECONDS", 0)

    async def scenario() -> bytes:
        broker = build_broker(get_settings())
        await broker.startup()
        try:
            await broker.kick(_probe(3))
            listener = broker.listen()
            await _ack(await _next_message(listener))
            with redis_from_env("TASKIQ_BROKER_URL") as r:
                r.flushdb()  # pyright: ignore[reportUnknownMemberType] - redis-py leaves `**kwargs` of flushdb() unannotated
            await broker.kick(_probe(4))
            message = await asyncio.wait_for(_next_message(listener), timeout=10)
            await listener.aclose()
            return message.data
        finally:
            await broker.shutdown()

    assert run_async(scenario()) == b"payload-4"


def test_failed_task_result_is_stored_as_json_with_a_ttl() -> None:
    """Results are JSON, never pickle, and an error survives the round trip."""
    error = TransientMailDeliveryError(TimeoutError())

    async def scenario() -> TaskiqResult[None]:
        # taskiq types `result_backend` with the unsolved type variable of
        # `AsyncBroker.__init__`; this test stores a result of None.
        backend = cast(
            "AsyncResultBackend[None]", build_broker(get_settings()).result_backend
        )
        try:
            await backend.set_result(
                "t-5",
                TaskiqResult[None](
                    is_err=True, return_value=None, execution_time=0.1, error=error
                ),
            )
            return await backend.get_result("t-5")
        finally:
            await backend.shutdown()

    loaded = run_async(scenario())

    with redis_from_env("TASKIQ_RESULT_BACKEND_URL") as r:
        raw = r.get("t-5")
        assert raw is not None
        stored = json.loads(raw)
        ttl = r.ttl("t-5")
    assert stored["is_err"] is True
    assert 0 < ttl <= TASK_RESULT_TTL_SECONDS
    assert loaded.is_err is True
    assert type(loaded.error).__name__ == "TransientMailDeliveryError"
    assert str(loaded.error) == "TimeoutError"


def _fail(exc: BaseException) -> None:
    """Let the worker's retry middleware handle a failed verification mail."""
    settings = get_settings()

    async def scenario() -> None:
        middleware = build_retry_middleware(settings, build_retry_source(settings))
        broker = build_broker(settings)
        middleware.set_broker(broker)
        message = TaskiqMessage(
            task_id="t-2",
            task_name=send_token_email_task.task_name,
            labels=dict(send_token_email_task.labels),
            args=[],
            kwargs={},
        )
        result = TaskiqResult[None](
            is_err=True, return_value=None, execution_time=0.1, error=exc
        )
        try:
            await middleware.on_error(message, result, exc)
        finally:
            await broker.shutdown()

    run_async(scenario())


def test_transient_failure_is_stored_as_a_json_retry_schedule() -> None:
    """The scheduler loads these values: JSON, never pickle."""
    _fail(TransientMailDeliveryError(TimeoutError()))

    with redis_from_env("TASKIQ_BROKER_URL") as r:
        [data_key] = redis_keys(r, f"{RETRY_SCHEDULE_PREFIX}:data:*")
        [time_key] = redis_keys(r, f"{RETRY_SCHEDULE_PREFIX}:time:*")
        raw = r.get(data_key)
        assert raw is not None
        schedule = json.loads(raw)
        queued_ids = r.lrange(time_key, 0, -1)

    assert schedule["task_name"] == "email.send_token_email"
    assert schedule["task_id"] == "t-2"
    assert schedule["time"] is not None
    assert queued_ids == [schedule["schedule_id"].encode()]


def test_permanent_failure_stores_no_retry() -> None:
    _fail(MailDeliveryError(TypeError()))

    with redis_from_env("TASKIQ_BROKER_URL") as r:
        assert redis_keys(r, f"{RETRY_SCHEDULE_PREFIX}:*") == []


def test_api_broker_declares_the_group_without_worker_middlewares(
    redis_broker_client: TestClient,
) -> None:
    """The API creates the consumer group at startup; no worker middleware.

    So tasks enqueued before any worker runs are delivered.
    """
    broker = app_of(redis_broker_client).state.broker

    with redis_from_env("TASKIQ_BROKER_URL") as r:
        groups = [g["name"] for g in r.xinfo_groups(TASK_STREAM)]

    assert groups == [b"taskiq"]
    assert broker.middlewares == []
    assert "PROMETHEUS_MULTIPROC_DIR" not in os.environ
