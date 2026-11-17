"""Routes read their settings through the `get_settings` dependency."""

from app.core.config import get_settings


class TestSettingsDependency:
    def test_routes_read_settings_through_the_dependency(self, client):
        override = get_settings().model_copy(update={"APP_NAME": "overridden"})
        client.app.dependency_overrides[get_settings] = lambda: override
        try:
            r = client.get("/metrics/health")
        finally:
            client.app.dependency_overrides.clear()

        assert r.status_code == 200
        assert r.json()["service"] == "overridden"
