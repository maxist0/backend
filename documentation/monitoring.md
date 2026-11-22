# Monitoring

The metrics, health endpoints and the monitoring stack of the backend. Form: reference, with an explanation of what is and is not wired. Logs are in [logging.md](logging.md).

## What exists today

| Signal | State |
|---|---|
| Metrics | Prometheus metrics of the API at `GET /metrics`, and of the worker on its own port |
| Health | `GET /health` and `GET /health/ready` |
| Logs | JSON on stdout, collected by Alloy and Loki ([logging.md](logging.md)) |
| Traces | **Not wired.** Tempo is deployed, but the application emits no spans (Roadmap Phase 3; FastAPI's telemetry is switched off in `create_app()`) |
| Alert rules | **None defined** in the repository (Roadmap Phase 3) |

## API metrics

`GET /metrics` returns the Prometheus text exposition of `prometheus_client`'s default registry. It is mounted when `METRICS_ENABLED` is true, is not part of the OpenAPI document, is not counted as a request and is unauthenticated (Roadmap Phase 1: protect it by network policy or token; the Swarm stack must not route it publicly). One process per container is assumed; several uvicorn workers in one container would need prometheus_client's multiprocess mode.

| Metric | Type | Labels | Defined in |
|---|---|---|---|
| `http_requests_total` | counter | `method`, `route`, `status` | `src/app/core/middleware.py` |
| `http_request_duration_seconds` | histogram (5 ms to 10 s buckets) | `method`, `route` | `src/app/core/middleware.py` |
| `app_info` | info | `name`, `version`, `environment` | `src/app/core/health.py` |
| `auth_login_failures_total` | counter | `reason`: `invalid_password`, `unknown_user`, `locked`, `inactive` | `src/app/modules/auth/service.py` |
| `auth_lockouts_total` | counter | none | `src/app/modules/auth/service.py` |
| `auth_refresh_token_reuse_total` | counter | none | `src/app/modules/auth/service.py` |
| `process_*`, `python_gc_*` | standard collectors | | `prometheus_client` |

- `route` is the route template (`/api/v1/users/me`, `/api/v1/notes/{note_id}`) or `unmatched`, so arbitrary request paths cannot create label values.
- `method` is limited to the standard HTTP methods plus `other`.
- There are no database-pool, cache or queue-length metrics yet (Roadmap Phase 3). Use `redis_exporter` for the stream backlog.

A module adds a metric by defining a `prometheus_client` metric at import and incrementing it from its service, as `auth` does. Define a label set with a fixed, small number of values.

## Worker metrics

taskiq's `PrometheusMiddleware` runs in the worker only and serves `GET /metrics` on `TASKIQ_METRICS_HOST`:`TASKIQ_METRICS_PORT` (default `127.0.0.1:9000`; a container sets `0.0.0.0` so the scraper on the monitoring network can reach it). Metrics, all with a `task_name` label (checked in the installed taskiq 0.13.0): `received_tasks`, `success_tasks`, `found_errors` (every failed attempt, retried ones included), `saved_results` and the `execution_time` histogram.

The worker processes write to prometheus_client's multiprocess directory `PROMETHEUS_MULTIPROC_DIR` (default `<tmp>/taskiq-metrics`, set by `app.worker` before anything imports prometheus_client). The first process to bind the port serves the sum of all processes. The directory must start empty, which a fresh container does. On a shared Linux host the default is a predictable path in `/tmp` (known issue 20).

## Health

| Endpoint | Meaning | Answer |
|---|---|---|
| `GET /health` | Liveness: the process serves requests; no I/O | `{"status": "ok"}` |
| `GET /health/ready` | Readiness: PostgreSQL `SELECT 1` and Redis `PING`, each bounded by `READINESS_TIMEOUT_SECONDS` (default 2 s) | 200 `{"status": "ok", "checks": {"database": "ok", "redis": "ok"}}`, or 503 with the failed check; never error text |

The `Dockerfile` healthcheck calls `/health`. Open: readiness does not flip to 503 during shutdown, there is no startup probe, and the Swarm stack routes `/health/ready` publicly (known issue 19).

## Monitoring stack

`deployment/docker/monitoring/docker-compose.yml` runs Prometheus, Grafana, Loki, Tempo, Alloy, cAdvisor and the Postgres and Redis exporters; its own README lists the ports. `deployment/` is git-ignored in this repository, and the stack has not been updated for this release (deploy round):

- `prometheus.yml` scrapes the API with `metrics_path: '/metrics/'`; the application serves `/metrics` and must be scraped there.
- Prometheus reaches the API and the worker through `host.docker.internal`; Roadmap Phase 6 moves it to Swarm service DNS.

## Example queries

These use the metric and label names above. They are standard PromQL and **untested** here (no Prometheus ran in this documentation pass).

```promql
# Requests per second by route
sum by (route) (rate(http_requests_total[5m]))

# Share of responses with a 5xx status
sum(rate(http_requests_total{status=~"5.."}[5m])) / sum(rate(http_requests_total[5m]))

# p95 latency by route
histogram_quantile(0.95, sum by (le, route) (rate(http_request_duration_seconds_bucket[5m])))

# Refused logins by reason
sum by (reason) (rate(auth_login_failures_total[5m]))

# Any reuse of a rotated refresh token
increase(auth_refresh_token_reuse_total[1h]) > 0
```

## Related

- [logging.md](logging.md): events and fields.
- [redis.md](redis.md): the worker, scheduler and retries.
- [ADR 0005](adr/0005-single-asgi-request-middleware.md): the request middleware that records the HTTP metrics.
