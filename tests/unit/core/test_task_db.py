"""`TaskDbSession`: the worker's task database, injected by taskiq."""

import logging
from collections.abc import Callable, Coroutine
from typing import Any, Never

import pytest
from taskiq import InMemoryBroker, TaskiqDepends, TaskiqResult, TaskiqState

import app.core.tasks as app_tasks
from app.core import TaskDbSession
from tests.helpers import run_async

# Any: the send and yield types of a task's coroutine are irrelevant here.
type _ProbeTask = Callable[[], Coroutine[Any, Any, None]]


class _Session:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.closed = True


class _Factory:
    def __init__(self) -> None:
        self.sessions: list[_Session] = []

    def __call__(self) -> _Session:
        session = _Session()
        self.sessions.append(session)
        return session


def _broker_with(factory: _Factory | None) -> InMemoryBroker:
    broker = InMemoryBroker(await_inplace=True)
    if factory is not None:
        broker.state["db_session_factory"] = factory
    return broker


def _run_task(broker: InMemoryBroker, func: _ProbeTask) -> TaskiqResult[None]:
    task = broker.task(task_name=f"probe.{func.__name__}")(func)

    async def _scenario() -> TaskiqResult[None]:
        await broker.startup()
        try:
            sent = await task.kiq()
            return await sent.wait_result()
        finally:
            await broker.shutdown()

    return run_async(_scenario())


class TestTaskDbSession:
    def test_task_gets_a_session_that_is_closed_afterwards(self) -> None:
        factory = _Factory()
        seen: list[object] = []

        async def uses_db(db: TaskDbSession = TaskiqDepends()) -> None:
            seen.append(db)

        result = _run_task(_broker_with(factory), uses_db)

        assert not result.is_err
        assert seen == factory.sessions
        assert factory.sessions[0].closed

    def test_failing_task_is_not_logged_again_by_the_dependency(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The dependency does not log the task's error a second time.

        taskiq-dependencies throws the task's error into the dependency and
        logs whatever comes back, message included.
        """
        factory = _Factory()

        async def fails(_db: TaskDbSession = TaskiqDepends()) -> None:
            raise RuntimeError("MARKER-secret")

        caplog.set_level(logging.DEBUG)
        result = _run_task(_broker_with(factory), fails)

        assert result.is_err
        assert factory.sessions[0].closed
        teardown = [
            r for r in caplog.records if r.name.startswith("taskiq.dependencies")
        ]
        assert teardown == []

    def test_outside_a_worker_the_task_fails_clearly(self) -> None:
        async def uses_db(_db: TaskDbSession = TaskiqDepends()) -> None:
            raise AssertionError("must not run")

        result = _run_task(_broker_with(None), uses_db)

        assert result.is_err
        assert "WORKER_STARTUP" in str(result.error)


class TestWorkerHooks:
    def test_unreachable_database_disposes_the_engine(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        disposed: list[bool] = []

        class _Engine:
            async def dispose(self) -> None:
                disposed.append(True)

        def _build_engine(_settings: object) -> _Engine:
            return _Engine()

        async def _init_db(_engine: object, _settings: object) -> Never:
            raise RuntimeError("Database connection failed")

        monkeypatch.setattr(app_tasks, "build_engine", _build_engine)
        monkeypatch.setattr(app_tasks, "init_db", _init_db)
        state = TaskiqState()

        with pytest.raises(RuntimeError, match="Database connection failed"):
            run_async(app_tasks.open_task_db(state))

        assert disposed == [True]
        assert dict(state) == {}

    def test_startup_stores_the_factory_and_shutdown_disposes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        closed: list[object] = []
        engine = object()

        def _build_engine(_settings: object) -> object:
            return engine

        async def _init_db(_engine: object, _settings: object) -> None:
            return None

        async def _close_db(engine: object) -> None:
            closed.append(engine)

        def _create_session_factory(bound: object) -> tuple[str, object]:
            return ("factory", bound)

        monkeypatch.setattr(app_tasks, "build_engine", _build_engine)
        monkeypatch.setattr(app_tasks, "init_db", _init_db)
        monkeypatch.setattr(app_tasks, "close_db", _close_db)
        monkeypatch.setattr(
            app_tasks, "create_session_factory", _create_session_factory
        )
        state = TaskiqState()

        run_async(app_tasks.open_task_db(state))
        assert state["db_session_factory"] == ("factory", engine)

        run_async(app_tasks.close_task_db(state))
        run_async(app_tasks.close_task_db(state))  # idempotent

        assert closed == [engine]
        assert dict(state) == {}
