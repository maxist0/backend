"""The test harness keeps the promises it makes."""

import asyncio
import sys

from alembic.config import Config
from alembic.script import ScriptDirectory

from tests.helpers import BACKEND_DIR, LOOP_FACTORY, new_test_client


def test_database_runs_the_production_major_version(db):
    assert 180000 <= int(db.scalar("SHOW server_version_num")) < 190000


def test_schema_is_exactly_the_alembic_head(db):
    """Schema ONLY via Alembic: the stamped revision is the script head."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    head = ScriptDirectory.from_config(cfg).get_current_head()

    assert db.scalar("SELECT version_num FROM alembic_version") == head


def test_state_is_empty_at_the_start_of_every_test(db):
    """TRUNCATE runs BEFORE each test, not after (see the next test)."""
    assert db.scalar("SELECT count(*) FROM users") == 0


def test_a_previous_tests_rows_do_not_leak_forward_part_1(client, captured_emails, db):
    from tests.helpers import register_ok

    register_ok(client, username="leaky")
    assert db.scalar("SELECT count(*) FROM users") == 1


def test_a_previous_tests_rows_do_not_leak_forward_part_2(db):
    """Runs after part 1 (file order): the row of part 1 must be gone."""
    assert db.scalar("SELECT count(*) FROM users") == 0


def test_testclient_requests_run_on_the_factory_loop(app):
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
