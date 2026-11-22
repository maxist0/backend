"""The test harness keeps the promises it makes.

The schema comes from Alembic, state never leaks from one test to the next,
requests run on the loop factory, and the app, the taskiq broker and the
Redis flush use the test containers.
"""

import asyncio
import os
import sys
from collections.abc import Callable

import pytest
import redis as redis_sync
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.helpers import (
    BACKEND_DIR,
    LOOP_FACTORY,
    Db,
    app_of,
    new_test_client,
    register_ok,
)


def test_database_runs_the_production_major_version(db: Db) -> None:
    assert 180000 <= int(db.scalar("SHOW server_version_num")) < 190000


def test_schema_is_exactly_the_alembic_head(db: Db) -> None:
    """Schema ONLY via Alembic: the stamped revision is the script head."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    assert db.scalar("SELECT version_num FROM alembic_version") == head


def test_state_is_empty_at_the_start_of_every_test(db: Db) -> None:
    """TRUNCATE runs BEFORE each test, not after (see the next test)."""
    assert db.scalar("SELECT count(*) FROM users") == 0


@pytest.mark.usefixtures("captured_emails")
def test_a_previous_tests_rows_do_not_leak_forward_part_1(
    client: TestClient, db: Db
) -> None:
    register_ok(client, username="leaky")
    assert db.scalar("SELECT count(*) FROM users") == 1


def test_a_previous_tests_rows_do_not_leak_forward_part_2(db: Db) -> None:
    """Runs after part 1 (file order): the row of part 1 must be gone."""
    assert db.scalar("SELECT count(*) FROM users") == 0


def test_testclient_requests_run_on_the_factory_loop(app: FastAPI) -> None:
    """The anyio portal uses LOOP_FACTORY: a selector loop on Windows.

    psycopg async cannot run on the Proactor loop, which is Windows' default
    since Python 3.8; without the factory the lifespan refuses to start.
    """

    @app.get("/_test/loop")
    async def loop_name() -> dict[str, str]:
        return {"loop": type(asyncio.get_running_loop()).__name__}

    with new_test_client(app) as client:
        name = client.get("/_test/loop").json()["loop"]

    if sys.platform == "win32":
        assert LOOP_FACTORY is asyncio.SelectorEventLoop
        assert "Selector" in name
        assert "Proactor" not in name
    else:
        assert LOOP_FACTORY is None


@pytest.mark.usefixtures("client")
def test_app_uses_the_redis_container_not_localhost_6379(app: FastAPI) -> None:
    """CACHE_HOST/CACHE_PORT must point at the container."""
    kwargs = app.state.redis.connection_pool.connection_kwargs
    assert kwargs["port"] == int(os.environ["CACHE_PORT"])
    assert kwargs["port"] != 6379


def test_taskiq_broker_uses_the_redis_container(
    redis_broker_client: TestClient,
) -> None:
    """The lifespan builds the broker from the wired settings."""
    broker = app_of(redis_broker_client).state.broker
    redis_port = int(os.environ["CACHE_PORT"])
    broker_kwargs = broker.connection_pool.connection_kwargs
    backend_kwargs = broker.result_backend.redis_pool.connection_kwargs
    assert broker_kwargs["port"] == redis_port
    assert broker_kwargs["db"] == 1
    assert backend_kwargs["port"] == redis_port
    assert backend_kwargs["db"] == 2


def test_flush_redis_fixture_empties_the_container(
    flush_redis: Callable[[], None],
) -> None:
    with redis_sync.Redis(port=int(os.environ["CACHE_PORT"])) as r:
        r.set("harness:marker", "1")
        assert r.get("harness:marker") == b"1"
        flush_redis()
        assert r.get("harness:marker") is None
