"""The HTTP metrics label a request with its full route template.

Included routers stay a tree: `scope["route"]` is the router's own `APIRoute`
without the include prefix. The request middleware resolves the full
template, so labels from different routers cannot collide (two routers with
`/me`).
"""

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY


def _request_count(route: str) -> float:
    """Sum of `http_requests_total` over all methods and statuses for `route`."""
    return sum(
        sample.value
        for metric in REGISTRY.collect()
        if metric.name == "http_requests"
        for sample in metric.samples
        if sample.name == "http_requests_total" and sample.labels.get("route") == route
    )


@pytest.mark.parametrize(
    ("method", "path", "router_local_path"),
    [
        ("GET", "/api/v1/users/me", "/me"),
        ("POST", "/api/v1/auth/login", "/login"),
        ("GET", "/health/ready", None),
        ("GET", "/", None),
    ],
)
def test_metrics_label_is_the_full_route_template(
    client: TestClient, method: str, path: str, router_local_path: str | None
) -> None:
    # Deltas, not label presence: the global registry is shared by all
    # tests, and the mounted admin app's own "/login" route exists too.
    full_before = _request_count(path)
    local_before = _request_count(router_local_path or "")

    client.request(method, path)

    assert _request_count(path) == full_before + 1
    assert _request_count(router_local_path or "") == local_before
