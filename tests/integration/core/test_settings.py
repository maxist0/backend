"""Routes read their settings through the `get_settings` dependency."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from tests.helpers import (
    app_of,
    login,
    register_ok,
)


class TestSettingsDependency:
    @pytest.mark.usefixtures("captured_emails")
    def test_routes_read_settings_through_the_dependency(
        self, client: TestClient
    ) -> None:
        """The cookie path comes from the route's `SettingsDep`.

        The routes stay mounted under the prefix the app was built with; only
        the value read per request changes.
        """
        register_ok(client, username="alice")
        override = get_settings().model_copy(update={"API_PREFIX": "/overridden"})
        app_of(client).dependency_overrides[get_settings] = lambda: override
        try:
            r = login(client, username="alice")
        finally:
            app_of(client).dependency_overrides.clear()

        assert r.status_code == 200
        assert "Path=/overridden/auth" in r.headers["set-cookie"]
