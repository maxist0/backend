"""The auth flows log no username, email address, token or database password."""

import logging
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from app.core import database
from app.core.config import get_settings
from tests.helpers import (
    MAXIMUM_LOGIN_ATTEMPTS,
    PII_EMAIL,
    PII_USERNAME,
    bearer,
    log_text,
    login,
    login_tokens,
    refresh_with,
    register,
    register_ok,
    run_async,
)


class TestLoggingHygiene:
    @pytest.mark.usefixtures("captured_emails")
    def test_auth_flows_do_not_log_username_or_email(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)

        register(client, username=PII_USERNAME, email=PII_EMAIL)
        login(client, username=PII_USERNAME, password="WrongPass!1")  # failed login
        login(client, username="ghost_" + PII_USERNAME)  # unknown user
        t = login_tokens(client, PII_USERNAME)
        client.get("/api/v1/users/me", headers=bearer(t.access))
        client.post("/api/v1/auth/password/forgot", json={"email": PII_EMAIL})
        refresh_with(client, t.refresh)
        for _ in range(MAXIMUM_LOGIN_ATTEMPTS):  # also exercises the lock log
            login(client, username=PII_USERNAME, password="WrongPass!1")
        client.cookies.clear()

        text = log_text(caplog)
        assert caplog.records, "no log records captured; test is not observing logs"
        assert PII_USERNAME not in text
        assert PII_EMAIL not in text

    @pytest.mark.usefixtures("captured_emails")
    def test_tokens_are_not_logged(
        self,
        client: TestClient,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level(logging.DEBUG)
        register_ok(client, username="alice")
        t = login_tokens(client, "alice")
        client.get("/api/v1/users/me", headers=bearer(t.access))
        refresh_with(client, t.refresh)
        text = log_text(caplog)
        assert t.refresh[:8] not in text
        assert t.access[:20] not in text

    def test_access_log_has_no_query_string_or_referer(
        self, client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.INFO)

        client.get(
            "/?token=SUPERSECRETQUERY",
            headers={"Referer": "https://example.org/?reset=SECRETREFERER"},
        )

        # Our records only: the test client logs the full URL itself.
        app_records = [r for r in caplog.records if r.name.startswith("app.")]
        text = "\n".join(r.getMessage() for r in app_records)
        assert "http.access" in text, "access log not observed"
        assert "SUPERSECRETQUERY" not in text
        assert "SECRETREFERER" not in text

    def test_database_url_password_is_not_logged(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Engine creation and init_db logs."""
        # The real test database with a wrong password: fails fast (an
        # unreachable port takes minutes on Windows) and the driver error
        # is a real authentication failure.
        password = "S3cr3tPw-Zq91xx"
        url = make_url(os.environ["DATABASE_URL"]).set(password=password)
        settings = get_settings().model_copy(
            update={"DATABASE_URL": url.render_as_string(hide_password=False)}
        )
        caplog.set_level(logging.DEBUG)

        engine = database.build_engine(settings)  # logs db.engine_creating

        async def _init_and_dispose() -> None:
            try:
                await database.init_db(engine, settings)  # fails, logs redacted
            finally:
                await engine.dispose()

        with pytest.raises(RuntimeError) as raised:
            run_async(_init_and_dispose())

        assert password not in str(raised.value)
        text = log_text(caplog)
        assert caplog.records
        assert password not in text
