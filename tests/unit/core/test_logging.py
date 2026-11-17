"""`setup_logging`: one stdout handler, renderer per environment, uvicorn routing."""

import json
import logging

import pytest

from app.core.config import get_settings
from app.core.logging import get_logger, setup_logging


@pytest.fixture
def restore_logging():
    """Re-apply the session's logging setup after the test changed it."""
    yield
    setup_logging(get_settings())


def _last_line(capsys) -> str:
    return capsys.readouterr().out.strip().splitlines()[-1]


class TestRenderer:
    def test_json_outside_dev(self, settings_factory, capsys, restore_logging):
        setup_logging(settings_factory(ENVIRONMENT="unittest"))

        get_logger("app.probe").info("probe.event", answer=42)

        record = json.loads(_last_line(capsys))
        assert record["event"] == "probe.event"
        assert record["answer"] == 42
        assert record["environment"] == "unittest"
        assert record["level"] == "info"
        assert {"timestamp", "hostname", "logger"} <= set(record)

    def test_console_in_dev(self, settings_factory, capsys, restore_logging):
        setup_logging(settings_factory(ENVIRONMENT="dev"))

        get_logger("app.probe").info("probe.event", answer=42)

        line = _last_line(capsys)
        assert "probe.event" in line
        assert "answer=42" in line
        with pytest.raises(json.JSONDecodeError):
            json.loads(line)

    def test_a_second_call_uses_the_new_settings(
        self, settings_factory, capsys, restore_logging
    ):
        """A logger used before the second call must not keep the old setup."""
        logger = get_logger("app.probe")
        setup_logging(settings_factory(ENVIRONMENT="unittest"))
        logger.info("probe.first")
        setup_logging(
            settings_factory(
                ENVIRONMENT="test",
                JWT_SECRET_KEY="k" * 48,
                FRONTEND_URL="https://app.example.com",
                ADMIN_ENABLED=False,
            )
        )

        logger.info("probe.second")

        record = json.loads(_last_line(capsys))
        assert (record["event"], record["environment"]) == ("probe.second", "test")

    def test_repeated_setup_keeps_one_handler(self, settings_factory, restore_logging):
        setup_logging(settings_factory())
        setup_logging(settings_factory())

        assert len(logging.getLogger().handlers) == 1


class TestStdlibLoggers:
    def test_uvicorn_errors_reach_the_root_handler(
        self, settings_factory, capsys, restore_logging
    ):
        # As after Alembic's fileConfig(), which disables existing loggers.
        logging.getLogger("uvicorn.error").disabled = True
        setup_logging(settings_factory())

        logging.getLogger("uvicorn.error").error("Application startup failed")

        assert json.loads(_last_line(capsys))["event"] == ("Application startup failed")
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            assert logging.getLogger(name).propagate is True
            assert logging.getLogger(name).handlers == []

    def test_uvicorn_access_lines_are_silenced(
        self, settings_factory, capsys, restore_logging
    ):
        """The request middleware's `http.access` replaces them."""
        setup_logging(settings_factory())
        capsys.readouterr()

        logging.getLogger("uvicorn.access").info('127.0.0.1 - "GET / HTTP/1.1" 200')

        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize(
        ("log_level", "expected"),
        [("DEBUG", logging.INFO), ("INFO", logging.INFO), ("WARNING", logging.WARNING)],
    )
    def test_taskiq_never_logs_below_info(
        self, settings_factory, capsys, restore_logging, log_level, expected
    ):
        """taskiq's DEBUG lines print task arguments (recipients, raw tokens)."""
        setup_logging(settings_factory(LOG_LEVEL=log_level))
        capsys.readouterr()

        logging.getLogger("taskiq.receiver.receiver").debug(
            "Received message: %s", "to=alice@example.com token=raw-token"
        )

        assert logging.getLogger("taskiq.kicker").getEffectiveLevel() == expected
        assert "raw-token" not in capsys.readouterr().out
