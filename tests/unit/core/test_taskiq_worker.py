"""Taskiq delivery, retries and worker metrics: guards on our configuration.

The worker root (`app.worker`) is imported only in a subprocess: importing it
sets `PROMETHEUS_MULTIPROC_DIR` and registers taskiq's metrics in the default
registry, which must never happen in an API process (or in this one).
"""

import asyncio
import datetime
import json
import os
import subprocess
import sys

import pytest
import redis.exceptions
from pydantic import ValidationError
from jinja2 import TemplateNotFound
from taskiq import AckableMessage, ScheduledTask, ScheduleSource
from taskiq.exceptions import NoResultError
from taskiq.message import TaskiqMessage
from taskiq.result import TaskiqResult
from taskiq_redis import RedisStreamBroker

import app.core.tasks as app_tasks
import app.modules  # noqa: F401  (registers the module tasks)
from app.core.config import get_settings
from app.core.mail import MailDeliveryError, TransientMailDeliveryError
from app.modules.auth.tasks import send_token_email_task
from tests.helpers import BACKEND_DIR, run_async


class RecordingSource(ScheduleSource):
    """In-memory schedule source: records what the retry middleware stores."""

    def __init__(self) -> None:
        self.added: list[ScheduledTask] = []

    async def get_schedules(self) -> list[ScheduledTask]:
        return list(self.added)

    async def add_schedule(self, schedule: ScheduledTask) -> None:
        self.added.append(schedule)


def _fail(exc: BaseException, retries: int = 0) -> tuple[RecordingSource, TaskiqResult]:
    """Let the configured retry middleware handle a failed verification mail."""
    settings = get_settings()
    source = RecordingSource()
    middleware = app_tasks.build_retry_middleware(settings, source)
    middleware.set_broker(app_tasks.build_broker(settings))
    labels = dict(send_token_email_task.labels)
    if retries:
        labels["_retries"] = retries
    message = TaskiqMessage(
        task_id="task-1",
        task_name=send_token_email_task.task_name,
        labels=labels,
        args=[],
        kwargs={},
    )
    result: TaskiqResult = TaskiqResult(
        is_err=True, return_value=None, execution_time=0.1, error=exc
    )
    run_async(middleware.on_error(message, result, exc))
    return source, result


class TestApiBroker:
    def test_is_a_bounded_stream_broker_with_result_ttl(self):
        broker = app_tasks.build_broker(get_settings())

        assert isinstance(broker, app_tasks.ReconnectingStreamBroker)
        # The former list key; XADD on it would fail with WRONGTYPE.
        assert broker.queue_name != "taskiq"
        assert broker.maxlen == app_tasks.TASK_STREAM_MAXLEN
        assert broker.approximate is True
        assert broker.consumer_id == "0"
        assert broker.result_backend.result_ex_time == app_tasks.TASK_RESULT_TTL_SECONDS

    def test_blocking_read_ends_before_the_socket_timeout(self):
        """An idle XREADGROUP must return before redis-py's read timeout."""
        broker = app_tasks.build_broker(get_settings())

        socket_timeout = broker.connection_pool.connection_kwargs["socket_timeout"]

        assert broker.block < socket_timeout * 1000

    def test_has_no_worker_middlewares_and_leaves_prometheus_alone(self, monkeypatch):
        """The API lifespan builds this broker; worker middlewares stay out.

        `PrometheusMiddleware` sets `PROMETHEUS_MULTIPROC_DIR`, which would put
        the API's own `/metrics` into multiprocess mode.
        """
        monkeypatch.delenv("PROMETHEUS_MULTIPROC_DIR", raising=False)

        broker = app_tasks.build_broker(get_settings())

        assert broker.middlewares == []
        assert "PROMETHEUS_MULTIPROC_DIR" not in os.environ


class TestRetries:
    def test_transient_error_is_retried_through_the_schedule_source(
        self, fresh_settings
    ):
        fresh_settings.setenv("TASKIQ_RETRY_DELAY_SECONDS", "42")
        before = datetime.datetime.now(datetime.UTC)

        source, result = _fail(TransientMailDeliveryError(TimeoutError()))

        [scheduled] = source.added
        delay = (scheduled.time - before).total_seconds()
        assert 42 <= delay <= 42 + 1 + 5  # jitter adds up to 1 s
        assert scheduled.task_name == "email.send_token_email"
        assert scheduled.task_id == "task-1"
        assert int(scheduled.labels["_retries"]) == 1
        # The failed attempt stores no result.
        assert isinstance(result.error, NoResultError)

    @pytest.mark.parametrize(
        "exc",
        [
            MailDeliveryError(TemplateNotFound("verify_email.html")),
            TypeError("bad call"),
            RuntimeError("bug"),
        ],
        ids=["permanent-mail-error", "type-error", "runtime-error"],
    )
    def test_other_errors_are_not_retried(self, exc):
        source, _ = _fail(exc)

        assert source.added == []

    def test_retry_count_comes_from_the_settings(self, fresh_settings):
        """`TASKIQ_RETRY_COUNT` retries follow the first run, then it stops."""
        fresh_settings.setenv("TASKIQ_RETRY_COUNT", "2")
        transient = TransientMailDeliveryError(ConnectionError())

        last_retry, _ = _fail(transient, retries=1)
        exhausted, _ = _fail(transient, retries=2)

        assert [int(s.labels["_retries"]) for s in last_retry.added] == [2]
        assert exhausted.added == []

    def test_retry_source_key_prefix_has_no_colon(self):
        """ListRedisScheduleSource parses `<prefix>:time:<time>` by colons."""
        assert ":" not in app_tasks.RETRY_SCHEDULE_PREFIX


class TestReconnect:
    """`listen` waits out what may pass and gives up on what cannot."""

    @staticmethod
    def _first_message(monkeypatch, first_error: BaseException) -> tuple[list, list]:
        """Fail the first read with `first_error`, then deliver one message."""
        reads: list[str] = []
        declares: list[str] = []

        async def flaky_listen(self):
            reads.append("listen")
            if len(reads) == 1:
                raise first_error
            yield AckableMessage(data=b"payload", ack=lambda: None)

        async def declare(self):
            declares.append("declare")

        monkeypatch.setattr(RedisStreamBroker, "listen", flaky_listen)
        monkeypatch.setattr(RedisStreamBroker, "_declare_consumer_group", declare)
        monkeypatch.setattr(app_tasks, "BROKER_RECONNECT_SECONDS", 0)
        broker = app_tasks.build_broker(get_settings())

        async def first() -> AckableMessage:
            listener = broker.listen()
            try:
                return await asyncio.wait_for(anext(listener), timeout=5)
            finally:
                await listener.aclose()

        assert run_async(first()).data == b"payload"
        return reads, declares

    def test_resumes_after_redis_was_unreachable(self, monkeypatch):
        """A Redis outage must not end `listen` (it would kill the worker)."""
        reads, _ = self._first_message(
            monkeypatch, redis.exceptions.ConnectionError("down")
        )

        assert reads == ["listen", "listen"]

    @pytest.mark.parametrize(
        "message",
        [
            "NOGROUP No such key 'taskiq:tasks' or consumer group 'taskiq'",
            "Command # 2 (XAUTOCLAIM taskiq:tasks taskiq c 600000 0-0 COUNT 100) "
            "of pipeline caused error: (\"NOGROUP No such key 'taskiq:tasks'\",)",
        ],
        ids=["xreadgroup", "xautoclaim-pipeline"],
    )
    def test_declares_the_group_again_when_it_is_gone(self, monkeypatch, message):
        """After FLUSHALL the connection lives on, but the group is gone."""
        reads, declares = self._first_message(
            monkeypatch, redis.exceptions.ResponseError(message)
        )

        assert reads == ["listen", "listen"]
        assert declares == ["declare", "declare"]

    @pytest.mark.parametrize(
        "error",
        [
            redis.exceptions.AuthenticationError("invalid password"),
            redis.exceptions.ResponseError("WRONGTYPE Operation against a key"),
        ],
        ids=["authentication", "other-error-reply"],
    )
    def test_errors_that_do_not_pass_by_waiting_propagate(self, monkeypatch, error):
        with pytest.raises(type(error)):
            self._first_message(monkeypatch, error)


class TestSettings:
    def test_metrics_server_listens_on_loopback_by_default(self, settings_factory):
        assert settings_factory().TASKIQ_METRICS_HOST == "127.0.0.1"

    @pytest.mark.parametrize("host", ["0.0.0.0", "::1", "metrics.internal", "worker"])
    def test_metrics_host_accepts_addresses_and_hostnames(self, settings_factory, host):
        assert settings_factory(TASKIQ_METRICS_HOST=host).TASKIQ_METRICS_HOST == host

    @pytest.mark.parametrize("host", ["", "bad host", "http://worker", "-worker"])
    def test_metrics_host_refuses_anything_else(self, settings_factory, host):
        with pytest.raises(ValidationError, match="TASKIQ_METRICS_HOST"):
            settings_factory(TASKIQ_METRICS_HOST=host)

    def test_retry_delay_is_at_least_thirty_seconds(self, settings_factory):
        with pytest.raises(ValidationError, match="TASKIQ_RETRY_DELAY_SECONDS"):
            settings_factory(TASKIQ_RETRY_DELAY_SECONDS=29)

        assert (
            settings_factory(TASKIQ_RETRY_DELAY_SECONDS=30).TASKIQ_RETRY_DELAY_SECONDS
            == 30
        )


@pytest.fixture(scope="module")
def worker_root(tmp_path_factory) -> dict:
    """Import `app.worker` in a fresh interpreter and report its wiring."""
    metrics_dir = tmp_path_factory.mktemp("taskiq-metrics")
    code = """
import asyncio, json
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.multiprocess import MultiProcessCollector
from taskiq.message import TaskiqMessage
from taskiq.middlewares import PrometheusMiddleware, SmartRetryMiddleware

import app.worker as w

retry = next(m for m in w.broker.middlewares if isinstance(m, SmartRetryMiddleware))
prom = next(m for m in w.broker.middlewares if isinstance(m, PrometheusMiddleware))
prom.pre_execute(TaskiqMessage(task_id="1", task_name="probe", labels={}, args=[], kwargs={}))
registry = CollectorRegistry()
MultiProcessCollector(registry, path=str(w.METRICS_DIR))
metrics = generate_latest(registry).decode()

seen = {}
def fake_run(coro, *, loop_factory=None):
    seen["loop_factory"] = loop_factory
    coro.close()
asyncio.run = fake_run
w.main()

print(json.dumps({
    "middlewares": sorted(type(m).__name__ for m in w.broker.middlewares),
    "retry_source_shared": retry.schedule_source is w.retry_source,
    "scheduler_reads_retries": w.scheduler.sources[-1] is w.retry_source,
    "retry_count": retry.default_retry_count,
    "retry_delay": retry.default_delay,
    "metrics_port": prom.server_port,
    "metrics_host": prom.server_addr,
    "metrics_dir": str(w.METRICS_DIR),
    "probe_counted": 'received_tasks_total{task_name="probe"} 1.0' in metrics,
    "selector_loop": seen["loop_factory"] is asyncio.SelectorEventLoop,
}))
"""
    env = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(metrics_dir),
        "TASKIQ_METRICS_PORT": "9123",
        "TASKIQ_RETRY_COUNT": "4",
        "TASKIQ_RETRY_DELAY_SECONDS": "45",
        "TASKIQ_METRICS_HOST": "0.0.0.0",
    }
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    report["expected_metrics_dir"] = str(metrics_dir)
    return report


class TestWorkerRoot:
    def test_worker_broker_has_retry_and_metrics_middlewares(self, worker_root):
        assert worker_root["middlewares"] == [
            "PrometheusMiddleware",
            "SmartRetryMiddleware",
        ]

    def test_settings_are_wired(self, worker_root):
        assert worker_root["retry_count"] == 5  # first run + TASKIQ_RETRY_COUNT
        assert worker_root["retry_delay"] == 45
        assert worker_root["metrics_port"] == 9123
        assert worker_root["metrics_host"] == "0.0.0.0"

    def test_scheduler_reads_the_retries_the_worker_writes(self, worker_root):
        assert worker_root["retry_source_shared"] is True
        assert worker_root["scheduler_reads_retries"] is True

    def test_task_metrics_reach_the_multiprocess_directory(self, worker_root):
        """The metrics server reads only these files; in-memory values are lost."""
        assert worker_root["metrics_dir"] == worker_root["expected_metrics_dir"]
        assert worker_root["probe_counted"] is True

    def test_entry_point_runs_on_a_selector_event_loop(self, worker_root):
        """psycopg cannot use Windows' default ProactorEventLoop."""
        assert worker_root["selector_loop"] is True


def test_scheduler_process_keeps_task_arguments_out_of_debug_logs(tmp_path):
    """The scheduler fires CLIENT_STARTUP, not WORKER_STARTUP.

    It runs like `taskiq scheduler --log-level DEBUG`: the CLI configures
    stdlib logging at DEBUG, then starts the worker-root broker. taskiq's
    kicker logs the arguments of every kicked task at DEBUG; our logging
    setup must clamp taskiq to INFO in this process too.
    """
    code = """
import asyncio, logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("taskiq").setLevel(logging.DEBUG)

import app.worker as w
from app.core.tasks import ReconnectingStreamBroker
from app.modules.auth.tasks import send_token_email_task

async def no_group(self):
    return None

ReconnectingStreamBroker._declare_consumer_group = no_group
w.broker.is_scheduler_process = True

async def main():
    await w.broker.startup()
    try:
        await send_token_email_task.kiq("MARKER-token-id")
    except Exception as exc:
        print("KICK-FAILED", type(exc).__name__)

asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
"""
    env = {
        **os.environ,
        "LOG_LEVEL": "DEBUG",
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        # Unreachable on purpose, also when the integration suite has pointed
        # the environment at its Redis container: the kick must fail after
        # the kicker has logged.
        "TASKIQ_BROKER_URL": "redis://127.0.0.1:1/1",
        "TASKIQ_RESULT_BACKEND_URL": "redis://127.0.0.1:1/2",
    }
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-2000:]
    # The kick was attempted (Redis is unreachable in unit tests) ...
    assert "KICK-FAILED" in output
    # ... and its arguments were never logged.
    assert "MARKER" not in output.replace("KICK-FAILED", "")


def test_scheduler_process_opens_no_task_database(tmp_path):
    """The scheduler builds the worker broker but fires CLIENT_STARTUP only.

    It must not open a database pool for `TaskDbSession`; a worker process
    must. `build_engine` is replaced by a probe, so nothing connects.
    """
    code = """
import asyncio

import app.core.tasks as core_tasks
import app.worker as w
from app.core.tasks import ReconnectingStreamBroker

class Opened(Exception):
    pass

def probe(settings):
    raise Opened()

async def no_group(self):
    return None

core_tasks.build_engine = probe
ReconnectingStreamBroker._declare_consumer_group = no_group

async def main():
    w.scheduler.broker.is_scheduler_process = True
    await w.scheduler.startup()
    print("SCHEDULER_STATE", sorted(w.broker.state.keys()))
    await w.broker.shutdown()
    w.broker.is_scheduler_process = False
    w.broker.is_worker_process = True
    try:
        await w.broker.startup()
    except Opened:
        print("WORKER_OPENS_DB")

asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
"""
    env = {
        **os.environ,
        "PROMETHEUS_MULTIPROC_DIR": str(tmp_path),
        "TASKIQ_BROKER_URL": "redis://127.0.0.1:1/1",
        "TASKIQ_RESULT_BACKEND_URL": "redis://127.0.0.1:1/2",
    }
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output[-2000:]
    assert "SCHEDULER_STATE []" in proc.stdout
    assert "WORKER_OPENS_DB" in proc.stdout


def test_built_broker_opens_the_task_database_in_worker_events_only():
    broker = app_tasks.build_broker(get_settings())

    assert (
        app_tasks.open_task_db
        in broker.event_handlers[app_tasks.TaskiqEvents.WORKER_STARTUP]
    )
    assert (
        app_tasks.close_task_db
        in broker.event_handlers[app_tasks.TaskiqEvents.WORKER_SHUTDOWN]
    )
    for event in (
        app_tasks.TaskiqEvents.CLIENT_STARTUP,
        app_tasks.TaskiqEvents.CLIENT_SHUTDOWN,
    ):
        handlers = broker.event_handlers.get(event, [])
        assert app_tasks.open_task_db not in handlers
        assert app_tasks.close_task_db not in handlers
