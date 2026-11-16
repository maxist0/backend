"""The app, the taskiq broker and the Redis flush use the test containers."""

import os


class TestHarness:
    def test_app_uses_the_redis_container_not_localhost_6379(self, client):
        """CACHE_HOST/CACHE_PORT must point at the container."""
        kwargs = client.app.state.cache.connection_pool.connection_kwargs
        assert kwargs["port"] == int(os.environ["CACHE_PORT"])
        assert kwargs["port"] != 6379

    def test_taskiq_broker_uses_the_redis_container(self):
        from app.common.taskiq import broker  # noqa: F401  (import for side effect check)
        from app.common.config import get_settings

        settings = get_settings()
        assert settings.TASKIQ_BROKER_URL == os.environ["TASKIQ_BROKER_URL"]
        assert (
            settings.TASKIQ_RESULT_BACKEND_URL
            == os.environ["TASKIQ_RESULT_BACKEND_URL"]
        )

    def test_flush_redis_fixture_empties_the_container(self, flush_redis):
        import redis as redis_sync

        r = redis_sync.Redis(port=int(os.environ["CACHE_PORT"]), decode_responses=True)
        try:
            r.set("harness:marker", "1")
            assert r.get("harness:marker") == "1"
            flush_redis()
            assert r.get("harness:marker") is None
        finally:
            r.close()
