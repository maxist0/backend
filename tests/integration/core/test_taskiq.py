"""Taskiq on a real Redis: delivery, acks that delete, retries, results, API broker."""

import asyncio
import contextlib
import json
import os

import redis as redis_sync
from taskiq.api import run_receiver_task
from taskiq.message import BrokerMessage, TaskiqMessage
from taskiq.result import TaskiqResult

import app.core.tasks as app_tasks

import app.modules  # noqa: F401  (registers the module tasks)
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
from tests.helpers import run_async


def _broker_redis() -> redis_sync.Redis:
    """Sync client on the broker's database (`TASKIQ_BROKER_URL`)."""
    return redis_sync.Redis.from_url(os.environ["TASKIQ_BROKER_URL"])


def _probe(n: int) -> BrokerMessage:
    return BrokerMessage(
        task_id=f"t-{n}", task_name="probe", message=f"payload-{n}".encode(), labels={}
    )


def _stream_state() -> tuple[int, int]:
    """Length of the task stream and number of pending entries."""
    with _broker_redis() as r:
        return r.xlen(TASK_STREAM), r.xpending(TASK_STREAM, "taskiq")["pending"]


def test_stream_message_is_pending_until_acknowledged_then_deleted():
    """At least once: unacknowledged entries stay; the ack deletes the entry,
    so a processed task's arguments do not linger in Redis."""

    async def scenario() -> tuple[bytes, tuple[int, int], tuple[int, int]]:
        broker = build_broker(get_settings())
        await broker.startup()
        try:
            await broker.kick(_probe(1))
            listener = broker.listen()
            message = await anext(listener)
            await listener.aclose()
            before = _stream_state()
            await message.ack()
            return message.data, before, _stream_state()
        finally:
            await broker.shutdown()

    data, before, after = run_async(scenario())

    assert data == b"payload-1"
    assert before == (1, 1)
    assert after == (0, 0)


def test_worker_deletes_the_entry_of_a_finished_task():
    """A real receiver runs the task, stores the result, acks: XLEN is 0."""

    async def scenario() -> tuple[int, int]:
        broker = build_broker(get_settings())
        await broker.startup()
        worker = asyncio.create_task(run_receiver_task(broker))
        try:
            await taskiq_heartbeat.kicker().with_broker(broker).kiq()
            async with asyncio.timeout(10):
                while _stream_state() != (0, 0):
                    await asyncio.sleep(0.05)
            return _stream_state()
        finally:
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            await broker.shutdown()

    assert run_async(scenario()) == (0, 0)


def test_reclaimed_entry_is_deleted_on_ack_too():
    """An entry left unacknowledged is claimed again (XAUTOCLAIM); its ack
    must delete it like the ack of a newly read entry."""

    async def scenario() -> tuple[bytes, tuple[int, int]]:
        broker = build_broker(get_settings())
        # Claim at once instead of after the production idle timeout.
        broker.idle_timeout = 0
        await broker.startup()
        try:
            await broker.kick(_probe(2))
            listener = broker.listen()
            await anext(listener)  # delivered, never acknowledged
            reclaimed = await anext(listener)
            await listener.aclose()
            await reclaimed.ack()
            return reclaimed.data, _stream_state()
        finally:
            await broker.shutdown()

    data, after = run_async(scenario())

    assert data == b"payload-2"
    assert after == (0, 0)


def test_listen_recovers_when_the_group_disappears(monkeypatch):
    """FLUSHALL keeps the connection but drops the consumer group (NOGROUP)."""
    monkeypatch.setattr(app_tasks, "BROKER_RECONNECT_SECONDS", 0)

    async def scenario() -> bytes:
        broker = build_broker(get_settings())
        await broker.startup()
        try:
            await broker.kick(_probe(3))
            listener = broker.listen()
            await (await anext(listener)).ack()
            with _broker_redis() as r:
                r.flushdb()
            await broker.kick(_probe(4))
            message = await asyncio.wait_for(anext(listener), timeout=10)
            await listener.aclose()
            return message.data
        finally:
            await broker.shutdown()

    assert run_async(scenario()) == b"payload-4"


def test_failed_task_result_is_stored_as_json_with_a_ttl():
    """Results are JSON, never pickle, and an error survives the round trip."""
    error = TransientMailDeliveryError(TimeoutError())

    async def scenario() -> TaskiqResult:
        backend = build_broker(get_settings()).result_backend
        try:
            await backend.set_result(
                "t-5",
                TaskiqResult(
                    is_err=True, return_value=None, execution_time=0.1, error=error
                ),
            )
            return await backend.get_result("t-5")
        finally:
            await backend.shutdown()

    loaded = run_async(scenario())

    with redis_sync.Redis.from_url(os.environ["TASKIQ_RESULT_BACKEND_URL"]) as r:
        stored = json.loads(r.get("t-5"))
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
        result: TaskiqResult = TaskiqResult(
            is_err=True, return_value=None, execution_time=0.1, error=exc
        )
        try:
            await middleware.on_error(message, result, exc)
        finally:
            await broker.shutdown()

    run_async(scenario())


def test_transient_failure_is_stored_as_a_json_retry_schedule():
    """The scheduler loads these values: JSON, never pickle."""
    _fail(TransientMailDeliveryError(TimeoutError()))

    with _broker_redis() as r:
        [data_key] = r.keys(f"{RETRY_SCHEDULE_PREFIX}:data:*")
        [time_key] = r.keys(f"{RETRY_SCHEDULE_PREFIX}:time:*")
        schedule = json.loads(r.get(data_key))
        queued_ids = r.lrange(time_key, 0, -1)

    assert schedule["task_name"] == "email.send_token_email"
    assert schedule["task_id"] == "t-2"
    assert schedule["time"] is not None
    assert queued_ids == [schedule["schedule_id"].encode()]


def test_permanent_failure_stores_no_retry():
    _fail(MailDeliveryError(TypeError()))

    with _broker_redis() as r:
        assert r.keys(f"{RETRY_SCHEDULE_PREFIX}:*") == []


def test_api_broker_declares_the_group_without_worker_middlewares(
    redis_broker_client,
):
    """The API creates the consumer group at startup, so tasks enqueued
    before any worker runs are delivered; it carries no worker middleware."""
    broker = redis_broker_client.app.state.broker

    with _broker_redis() as r:
        groups = [g["name"] for g in r.xinfo_groups(TASK_STREAM)]

    assert groups == [b"taskiq"]
    assert broker.middlewares == []
    assert "PROMETHEUS_MULTIPROC_DIR" not in os.environ
