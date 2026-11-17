"""Metrics endpoints: Prometheus exposition format and health payload."""


def test_metrics_endpoint_prometheus_format(client):
    resp = client.get("/metrics")

    assert resp.status_code == 200, (
        f"Expected 200 but got {resp.status_code}. Response: {resp.text}"
    )

    content_type = resp.headers.get("content-type", "")
    assert content_type.startswith("text/plain"), (
        f"Expected prometheus text content-type, got {content_type}"
    )

    # Response body should be bytes in Prometheus exposition format and contain metric help/values
    body = resp.content
    assert isinstance(body, (bytes, bytearray)), (
        f"Expected bytes body, got {type(body)}"
    )
    assert b"# HELP" in body or b"http_requests_total" in body, (
        f"Metrics output looks unexpected: {body[:200]!r}"
    )


def test_metrics_health_endpoint(client):
    resp = client.get("/metrics/health")

    assert resp.status_code == 200, (
        f"Expected 200 but got {resp.status_code}. Response: {resp.text}"
    )

    content_type = resp.headers.get("content-type", "")
    assert content_type.startswith("application/json"), (
        f"Expected JSON, got {content_type}"
    )

    body = resp.json()
    assert body["status"] == "healthy"
    assert body["metrics_available"] is True
    assert "success" not in body  # no envelope
