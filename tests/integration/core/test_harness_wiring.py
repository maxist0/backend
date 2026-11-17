"""The app, the taskiq broker and the Redis flush use the test containers."""

import os


class TestHarness:
    def test_app_uses_the_redis_container_not_localhost_6379(self, client):
        """CACHE_HOST/CACHE_PORT must point at the container."""
        kwargs = client.app.state.redis.connection_pool.connection_kwargs
        assert kwargs["port"] == int(os.environ["CACHE_PORT"])
        assert kwargs["port"] != 6379

    def test_taskiq_broker_uses_the_redis_container(self, redis_broker_client):
        """The lifespan builds the broker from the wired settings."""
        broker = redis_broker_client.app.state.broker
        redis_port = int(os.environ["CACHE_PORT"])
        broker_kwargs = broker.connection_pool.connection_kwargs
        backend_kwargs = broker.result_backend.redis_pool.connection_kwargs
        assert broker_kwargs["port"] == redis_port
        assert broker_kwargs["db"] == 1
        assert backend_kwargs["port"] == redis_port
        assert backend_kwargs["db"] == 2

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
