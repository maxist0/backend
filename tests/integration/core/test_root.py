"""The root endpoint answers with the API entry points."""

import pytest


@pytest.mark.root
def test_root_lists_the_entry_points(client):
    resp = client.get("/")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert set(body) == {"message", "docs_url", "metrics_url", "auth_url"}
    assert body["auth_url"] == "/api/v1/auth"
    assert body["metrics_url"] == "/metrics"
