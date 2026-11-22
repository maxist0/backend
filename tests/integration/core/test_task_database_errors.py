"""A task's database error leaves no row value in the worker log or Redis."""

import logging

import pytest
from taskiq import InMemoryBroker, TaskiqDepends, TaskiqResult

from app.core import TaskDbSession, task
from app.core.config import get_settings
from app.core.tasks import TaskDatabaseError, add_task_db_hooks, build_broker
from app.modules.users import User
from tests.helpers import (
    PII_EMAIL,
    redis_from_env,
    run_async,
)


@task(task_name="probe.duplicate_email")
async def duplicate_email(db: TaskDbSession = TaskiqDepends()) -> None:
    """Insert two users with one address: the second violates `users_email_key`."""
    for username in ("probe_first", "probe_second"):
        db.add(User(username=username, email=PII_EMAIL))
        await db.commit()


def test_unique_violation_text_reaches_neither_log_nor_stored_result(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Alembic's fileConfig (run by the `migrated_db` fixture) disables every
    # logger that exists at that moment, taskiq's receiver included; a worker
    # process never runs it.
    monkeypatch.setattr(
        logging.getLogger("taskiq.receiver.receiver"), "disabled", False
    )
    settings = get_settings()
    # Worker hooks for the task database, results in Redis as in production.
    broker = InMemoryBroker(await_inplace=True).with_result_backend(
        build_broker(settings).result_backend
    )
    add_task_db_hooks(broker)

    async def _scenario() -> tuple[str, TaskiqResult[None]]:
        await broker.startup()
        try:
            sent = await duplicate_email.kicker().with_broker(broker).kiq()
            return sent.task_id, await sent.wait_result()
        finally:
            await broker.shutdown()

    caplog.set_level(logging.DEBUG)
    task_id, result = run_async(_scenario())

    assert result.is_err
    assert type(result.error) is TaskDatabaseError
    assert str(result.error) == "IntegrityError (UniqueViolation, SQLSTATE 23505)"
    with redis_from_env("TASKIQ_RESULT_BACKEND_URL") as r:
        stored = r.get(task_id)
    assert isinstance(stored, bytes)
    assert PII_EMAIL.encode() not in stored
    logged = "\n".join(
        f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records
    )
    assert "Exception found while executing function" in logged
    assert PII_EMAIL not in logged
