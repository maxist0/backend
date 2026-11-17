"""The `task` decorator keeps database error text out of taskiq's log and results."""

import inspect
import logging

import psycopg
import psycopg.errors
import pytest
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from taskiq import InMemoryBroker, ScheduledTask, ScheduleSource, TaskiqDepends

import app.core.tasks as app_tasks
from app.core import TaskDbSession, task
from app.core.config import get_settings
from app.core.mail import TransientMailDeliveryError
from tests.helpers import run_async

MARKER = "zebra.mail91@example.com"


class _DriverError(Exception):
    """Stands in for a psycopg error: a message with row data and a SQLSTATE."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(f"DETAIL: Key (email)=({MARKER}) already exists.")
        self.sqlstate = sqlstate


def _db_error(sqlstate: str | None, cls: type[DBAPIError] = IntegrityError):
    return cls("INSERT INTO users ...", None, _DriverError(sqlstate))


class _RecordingSource(ScheduleSource):
    def __init__(self) -> None:
        self.added: list[ScheduledTask] = []

    async def get_schedules(self) -> list[ScheduledTask]:
        return list(self.added)

    async def add_schedule(self, schedule: ScheduledTask) -> None:
        self.added.append(schedule)


class TestClassification:
    @pytest.mark.parametrize(
        "error",
        [
            _db_error("40001"),  # serialization_failure
            _db_error("40P01"),  # deadlock_detected
            _db_error("57P01"),  # admin_shutdown
            _db_error("08006"),  # connection_failure
            _db_error("08001"),  # sqlclient_unable_to_establish_sqlconnection
            _db_error(None, OperationalError),  # no answer from the server
            psycopg.errors.DeadlockDetected(),
            psycopg.OperationalError("connection refused"),
        ],
        ids=[
            "40001",
            "40P01",
            "57P01",
            "08006",
            "08001",
            "operational-no-sqlstate",
            "raw-deadlock",
            "raw-operational",
        ],
    )
    def test_passing_errors_are_transient(self, error):
        assert isinstance(
            app_tasks.task_database_error(error), app_tasks.TransientTaskDatabaseError
        )

    @pytest.mark.parametrize(
        "error",
        [
            _db_error("23505"),  # unique_violation
            _db_error("42P01"),  # undefined_table
            _db_error(None),
            psycopg.errors.UniqueViolation(),
        ],
        ids=["23505", "42P01", "integrity-no-sqlstate", "raw-unique"],
    )
    def test_other_errors_are_permanent(self, error):
        replaced = app_tasks.task_database_error(error)

        assert type(replaced) is app_tasks.TaskDatabaseError

    def test_invalidated_connection_is_transient(self):
        error = DBAPIError(
            "SELECT 1", None, _DriverError(None), connection_invalidated=True
        )

        assert isinstance(
            app_tasks.task_database_error(error), app_tasks.TransientTaskDatabaseError
        )

    def test_message_holds_only_types_and_sqlstate(self):
        replaced = app_tasks.task_database_error(_db_error("23505"))

        assert str(replaced) == "IntegrityError (_DriverError, SQLSTATE 23505)"

    def test_rebuilds_from_its_message(self):
        """taskiq rebuilds a stored error as `cls(*args)`."""
        replaced = app_tasks.task_database_error(_db_error("40P01"))

        assert str(type(replaced)(*replaced.args)) == str(replaced)


def _run(func, *, retry: bool = False):
    """Run `func` as a shared task through an in-memory worker.

    Returns the stored result (None when the retry middleware scheduled a
    retry, which stores none) and the retry schedules.
    """
    settings = get_settings()
    source = _RecordingSource()
    broker = InMemoryBroker(await_inplace=True)
    broker.add_middlewares(app_tasks.build_retry_middleware(settings, source))
    labels = {"retry_on_error": True} if retry else {}
    decorated = task(task_name=f"probe.{func.__name__}", **labels)(func)

    async def _scenario():
        await broker.startup()
        try:
            # The inline broker has run the task when `kiq` returns.
            sent = await decorated.kicker().with_broker(broker).kiq()
            return broker.result_backend.results.get(sent.task_id)
        finally:
            await broker.shutdown()

    return run_async(_scenario()), source


class TestTaskDecorator:
    def test_database_error_text_reaches_neither_log_nor_result(
        self, caplog, monkeypatch
    ):
        async def violates_unique() -> None:
            raise _db_error("23505")

        # The integration run's Alembic fileConfig disables existing loggers,
        # taskiq's receiver included; a worker process never runs it.
        monkeypatch.setattr(
            logging.getLogger("taskiq.receiver.receiver"), "disabled", False
        )
        caplog.set_level(logging.DEBUG)
        result, _ = _run(violates_unique)

        assert type(result.error) is app_tasks.TaskDatabaseError
        assert result.error.__context__ is None
        assert result.error.__cause__ is None
        logged = "\n".join(
            f"{r.getMessage()} {r.exc_text or ''}" for r in caplog.records
        )
        assert "Exception found while executing function" in logged
        assert MARKER not in logged
        assert MARKER not in repr(result.error)

    def test_deadlock_is_scheduled_for_a_retry(self):
        async def deadlocks() -> None:
            raise _db_error("40P01")

        _, source = _run(deadlocks, retry=True)

        [scheduled] = source.added
        assert scheduled.task_name == "probe.deadlocks"

    def test_permanent_database_error_is_not_retried(self):
        async def violates_unique_again() -> None:
            raise _db_error("23505")

        _, source = _run(violates_unique_again, retry=True)

        assert source.added == []

    def test_other_errors_pass_through_unchanged(self):
        """The mail classification still works through the wrapper."""
        error = TransientMailDeliveryError(TimeoutError())

        async def mail_times_out() -> None:
            raise error

        result, source = _run(mail_times_out, retry=True)

        assert len(source.added) == 1
        assert result is None  # retried: the wrapper let the mail error through

    def test_non_database_error_keeps_its_type_and_identity(self):
        error = RuntimeError("bug")

        async def has_a_bug() -> None:
            raise error

        result, _ = _run(has_a_bug)

        assert result.error is error

    def test_signature_dependencies_and_labels_are_kept(self):
        async def needs_db(user_id: str, db: TaskDbSession = TaskiqDepends()) -> None:
            return None

        decorated = task(
            task_name="probe.needs_db",
            retry_on_error=True,
            # Not `schedule`: the task stays in taskiq's global registry, and
            # the scheduler test expects only the heartbeat schedule.
            timeout=30,
        )(needs_db)

        assert decorated.task_name == "probe.needs_db"
        assert decorated.labels == {
            "retry_on_error": True,
            "timeout": 30,
        }
        assert decorated.original_func.__wrapped__ is needs_db
        assert list(inspect.signature(decorated.original_func).parameters) == [
            "user_id",
            "db",
        ]
