# Monitoring and Observability

> **Status (2026-11-16):** this page is partly stale; the metric names and labels in the examples below (`path`, `db_query_total`, `cache_*`, `auth_*`) do not match the code. The metrics that exist are listed in [README.md → Observability](../README.md#observability) and defined in `src/app/core/middleware.py`, `src/app/core/health.py` and `src/app/modules/auth/service.py`. Verified corrections (2026-11-17): the endpoint is `GET /metrics`; the label for the request path is `route` (not `path`), and its value is the route template or `unmatched`; the status label is `status`; the `active_refresh_tokens` gauge was removed; logs no longer contain usernames, emails or query strings.

## Overview

The application implements comprehensive monitoring covering **metrics** (Prometheus), **logs** (structured + OTLP/Loki), **traces** (OpenTelemetry), and **alerts** (Grafana).

The monitoring stack includes:

- **Prometheus**: Scrapes metrics from `/metrics` endpoint
- **Grafana**: Visualizes metrics and logs
- **Loki**: Log aggregation from OTLP export
- **Tempo**: Trace backend for OpenTelemetry spans
- **Alloy**: OTLP receiver and log processor
- **OpenTelemetry**: Log and trace exporting

## Monitoring Architecture

```
Application
├── Metrics (Prometheus client library)
│   └─ Exported via GET /metrics
│     └─ Scraped by Prometheus
│       └─ Visualized in Grafana
│
├── Structured Logs
│   ├─ Console output (dev)
│   └─ OTLP export (stage/prod)
│     └─ Grafana Alloy receives
│       └─ Stored in Loki
│         └─ Queried in Grafana
│
└── Traces
    └─ OpenTelemetry SDK
      └─ Grafana Alloy OTLP receiver
        └─ Tempo
```

## Prometheus Metrics

### Metrics Endpoint

All metrics are exposed at:

```
GET http://localhost:8000/metrics
```

Available in development, staging, and production when `METRICS_ENABLED=True`.

### Application Metrics

#### Request Metrics

```
# Counter: Total HTTP requests
http_requests_total{method="GET", path="/api/users", status="200"} 1042

# Histogram: Request duration
http_request_duration_seconds{method="GET", path="/api/users", le="0.025"} 890
http_request_duration_seconds{method="GET", path="/api/users", le="0.05"} 920
http_request_duration_seconds{method="GET", path="/api/users", le="+Inf"} 1042

# Gauge: Requests in progress
http_requests_inprogress{method="GET", path="/api/users"} 2
```

#### Database Metrics

```
# Counter: Total database queries
db_query_total{operation="SELECT", table="users", status="success"} 5432
db_query_total{operation="SELECT", table="users", status="error"} 12

# Histogram: Query duration
db_query_duration_seconds{operation="SELECT", table="users", le="0.05"} 4200
db_query_duration_seconds{operation="SELECT", table="users", le="0.1"} 5300

# Gauge: Active connections
db_connections_active{pool="default"} 8
db_connections_idle{pool="default"} 2
```

#### Cache Metrics

```
# Counter: Cache operations
cache_hits_total{operation="get"} 14521
cache_misses_total{operation="get"} 2341

# Gauge: Cache size
cache_size_bytes{key_pattern="user:*"} 1024000
cache_keys_total{key_pattern="*"} 3421
```

#### Authentication Metrics

```
# Counter: Auth events
auth_login_total{result="success"} 523
auth_login_total{result="failure"} 42
auth_login_total{result="account_locked"} 3

# Gauge: Locked accounts
auth_locked_accounts_total 15
```

### Application Info

Metadata about the running application:

```
# Gauge: Application info
app_info{app_name="api", version="0.1.0", environment="prod"} 1

# Gauge: Python info
python_info{version="3.10.0"} 1
```

## Prometheus Configuration

### Basic Scrape Config

```yaml
# deployment/docker/monitoring/prometheus/prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "api"
    static_configs:
      - targets: ["api:8000"]
    metrics_path: /metrics
    scrape_interval: 10s
```

### Kubernetes ServiceMonitor

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: api-metrics
  namespace: production
spec:
  selector:
    matchLabels:
      app: api
  endpoints:
  - port: metrics
    interval: 10s
    path: /metrics
```

## Grafana Dashboards

### Default Dashboards

Create dashboards for:

1. **Application Overview**
   - Requests per second (RPS)
   - Error rate (4xx, 5xx)
   - Response time (p50, p95, p99)
   - Active connections

2. **Database Health**
   - Query performance (p50, p95, p99)
   - Connection pool usage
   - Slow queries (> 1 second)
   - Query errors

3. **Cache Performance**
   - Hit rate
   - Miss rate
   - Cache size
   - Evictions

4. **Authentication & Security**
   - Login success/failure rate
   - Locked accounts trend
   - Failed login attempts by IP

### Example Dashboard Query

```promql
# Request rate (requests per second)
rate(http_requests_total[5m])

# Error rate percentage
(
  rate(http_requests_total{status=~"5.."}[5m]) /
  rate(http_requests_total[5m])
) * 100

# P95 response time
histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))

# Cache hit rate
rate(cache_hits_total[5m]) / (rate(cache_hits_total[5m]) + rate(cache_misses_total[5m]))
```

## Log Aggregation

### OTLP Log Export

In production and staging, logs are exported to Grafana Alloy:

```ini
# .env.prod
OTLP_LOGS_ENABLED=True
OTLP_ENDPOINT=http://grafana-alloy:4318
OTLP_SERVICE_NAME=api
OTLP_HEADERS=Authorization=Bearer token
```

### Alloy Log Receiver

```alloy
# deployment/docker/monitoring/alloy/config.alloy
otelreceiver.otlp "default" {
  protocols {
    http {
      endpoint = "0.0.0.0:4318"
    }
  }

  output {
    logs = [loki.write.default.receiver]
  }
}

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
```

### Loki Log Queries

Query logs in Grafana using LogQL:

```logql
# All logs from API service
{job="api"}

# Error logs
{job="api"} | json | level="error"

# Auth-related events
{job="api"} | json | event =~ "auth.*"

# Login failures
{job="api"} | json | event="auth.login_failed" | ip_address=""

# Slow database queries
{job="api"} | json | operation="SELECT" | duration_ms > 1000

# Count login attempts by user
{job="api"} | json | event="auth.login_success" | stats count() by user_id
```

## Alerting

### Alert Rules (Prometheus)

```yaml
# deployment/docker/monitoring/prometheus/alerts.yml
groups:
- name: api_alerts
  interval: 1m
  rules:
  
  # High error rate
  - alert: HighErrorRate
    expr: |
      (
        sum(rate(http_requests_total{status=~"5.."}[5m]))
        /
        sum(rate(http_requests_total[5m]))
      ) > 0.05
    for: 5m
    labels:
      severity: critical
    annotations:
      summary: "High error rate detected"
      description: "Error rate is {{ $value | humanizePercentage }}"
  
  # Slow response time
  - alert: SlowResponseTime
    expr: |
      histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m])) > 1
    for: 10m
    labels:
      severity: warning
    annotations:
      summary: "High response latency"
      description: "P95 response time is {{ $value }}s"
  
  # High database connection pool usage
  - alert: DBConnectionPoolExhaustion
    expr: |
      (db_connections_active / (db_connections_active + db_connections_idle)) > 0.9
    for: 5m
    labels:
      severity: warning
    annotations:
      summary: "Database connection pool near capacity"
  
  # Cache hit rate too low
  - alert: LowCacheHitRate
    expr: |
      (
        rate(cache_hits_total[5m])
        /
        (rate(cache_hits_total[5m]) + rate(cache_misses_total[5m]))
      ) < 0.7
    for: 10m
    labels:
      severity: info
    annotations:
      summary: "Cache hit rate below 70%"
  
  # Multiple failed login attempts
  - alert: BruteForceAttempt
    expr: |
      sum(rate(auth_login_total{result="failure"}[1m])) by (ip_address) > 10
    for: 1m
    labels:
      severity: warning
    annotations:
      summary: "Potential brute force attack from {{ $labels.ip_address }}"
      description: "{{ $value }} failed login attempts per minute"
```

### Grafana Alert Notifications

Configure notification channels:

1. **Email**: Alert recipients get email notifications
2. **Slack**: Post alerts to Slack channel
3. **PagerDuty**: Trigger incidents for critical alerts
4. **Webhook**: Send to custom endpoint

### Alert Response Procedures

| Alert | Severity | Response |
|-------|----------|----------|
| High error rate (> 5%) | Critical | Immediate investigation |
| Slow response time (P95 > 1s) | Warning | Monitor, optimize |
| Connection pool exhaustion | Warning | Check for connection leaks |
| Brute force attempts | Warning | Review logs, block IP |
| Cache hit rate < 70% | Info | Review cache strategy |

## Health Checks

### Liveness Check

```
GET /health
```

Returns 200 if application is running:

```json
{
  "status": "alive",
  "timestamp": "2026-02-27T10:00:00Z"
}
```

### Readiness Check

```
GET /health/ready
```

Returns 200 if all dependencies are ready:

```json
{
  "status": "ready",
  "database": "connected",
  "cache": "connected",
  "timestamp": "2026-02-27T10:00:00Z"
}
```

### Kubernetes Probes

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: api
spec:
  template:
    spec:
      containers:
      - name: api
        livenessProbe:
          httpGet:
            path: /health
            port: 8000
          initialDelaySeconds: 10
          periodSeconds: 10

        readinessProbe:
          httpGet:
            path: /health/ready
            port: 8000
          initialDelaySeconds: 5
          periodSeconds: 5
```

## Performance Monitoring

### Key Performance Indicators (KPIs)

| KPI | Target | Alert Threshold |
|-----|--------|-----------------|
| Error Rate | < 1% | > 5% |
| Response Time (P95) | < 500ms | > 1s |
| Uptime | > 99.5% | < 99% |
| Cache Hit Rate | > 80% | < 70% |
| Database Connection Pool | < 70% utilized | > 90% |

### Dashboard Queries

**SLO Dashboard**:
```promql
# Uptime percentage
(
  count(up{job="api"} == 1) / count(up{job="api"})
) * 100

# Monthly request success rate
(
  sum(rate(http_requests_total{status!~"5.."}[30d]))
  /
  sum(rate(http_requests_total[30d]))
) * 100
```

## Debugging with Logs

### Filtering by Event Type

```logql
# Find all auth errors
{job="api"} | json | event =~ "auth.*" level="error"

# Find specific user activity
{job="api"} | json | user_id="550e8400-e29b-41d4-a716-446655440000"

# Find errors in specific API path
{job="api"} | json | path="/api/auth/login" level="error"
```

### Analyzing Performance

```logql
# Slowest database queries
{job="api"} | json | operation="SELECT" | line_format "{{ .duration_ms }}" | quantile_over_time(0.95, [5m])

# Most common errors
{job="api"} | json | level="error" | stats count() by error_code
```

### Security Analysis

```logql
# Failed login trends
{job="api"} | json | event="auth.login_failed" | stats count() by ip_address

# Suspicious IP addresses
{job="api"} | json | event =~ "auth.*" | stats count() by ip_address | > 100
```

## Container Monitoring

### Docker Compose Monitoring

```yaml
services:
  prometheus:
    image: prom/prometheus:latest
    ports:
      - 9090:9090
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - ./alerts.yml:/etc/prometheus/alerts.yml

  grafana:
    image: grafana/grafana:latest
    ports:
      - 3000:3000
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin
    volumes:
      - ./grafana/provisioning:/etc/grafana/provisioning

  loki:
    image: grafana/loki:latest
    ports:
      - 3100:3100

  alloy:
    image: grafana/alloy:latest
    ports:
      - 4318:4318
    volumes:
      - ./alloy/config.alloy:/etc/alloy/config.alloy
```

Access Grafana at `http://localhost:3000` (admin/admin)

---

**Related Documentation:**
- See [logging.md](logging.md) for structured logging
- See [environment.md](environment.md) for environment-specific monitoring settings
