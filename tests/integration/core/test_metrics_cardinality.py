"""Metric label cardinality stays bounded whatever the client sends."""

import uuid


class TestMetricsCardinality:
    def test_unmatched_paths_collapse_into_one_label(self, client):
        random_segments = [uuid.uuid4().hex for _ in range(5)]
        for segment in random_segments:
            assert client.get(f"/zz-{segment}").status_code == 404

        body = client.get("/metrics").text

        for segment in random_segments:
            assert segment not in body, "raw 404 path leaked into a metric label"
        assert 'endpoint="unmatched"' in body

    def test_matched_route_uses_its_path_template(self, client):
        """GUARD: real routes keep a bounded, meaningful label."""
        client.post(
            "/api/v1/auth/login", data={"username": "ghost", "password": "x" * 10}
        )
        body = client.get("/metrics").text
        assert 'endpoint="/api/v1/auth/login"' in body

    def test_unknown_http_method_collapses_into_the_other_label(self, client):
        """The method label comes from the client, so it must be bounded."""
        invented = "ZZ" + uuid.uuid4().hex[:12].upper()

        client.request(invented, "/")
        body = client.get("/metrics").text

        assert invented not in body, "client-chosen method leaked into a metric label"
        assert 'method="other"' in body

    def test_standard_methods_keep_their_label(self, client):
        """GUARD: the bounded set still distinguishes the real methods."""
        client.get("/")
        client.post(
            "/api/v1/auth/login", data={"username": "ghost", "password": "x" * 10}
        )
        body = client.get("/metrics").text
        assert 'method="GET"' in body
        assert 'method="POST"' in body
