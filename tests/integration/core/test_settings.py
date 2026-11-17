"""Routes read their settings through the `get_settings` dependency."""

from app.core.config import get_settings
from tests.helpers import login, register_ok


class TestSettingsDependency:
    def test_routes_read_settings_through_the_dependency(self, client, captured_emails):
        register_ok(client, username="alice")
        override = get_settings().model_copy(update={"ACCESS_TOKEN_EXPIRE_MINUTES": 7})
        client.app.dependency_overrides[get_settings] = lambda: override
        try:
            r = login(client, username="alice")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 200
        assert r.json()["expires_in"] == 7 * 60
