# Logging Architecture

## Overview

This project uses **structlog** for structured logging with optional OpenTelemetry Protocol (OTLP) export to Grafana Alloy for centralized log collection and analysis. Logging is configured separately for development and production environments.

## Logging Stack

```
Application Code
    ↓ (structlog.get_logger())
Structlog Processors (timestamp, log_level, exc_info, context)
    ↓
stdlib logging bridge (ProcessorFormatter)
    ├→ StreamHandler (console output)
    │   ├ DEV:  Rich ConsoleRenderer (colored, human-readable)
    │   └ PROD: JSONRenderer (machine-readable)
    │
    └→ OTLP Handler (when OTLP_LOGS_ENABLED=True)
        ├ opentelemetry-sdk
        └ OTLPLogExporter → HTTP → Grafana Alloy → Loki
```

## Configuration

Logging is configured in [src/common/logging.py](../src/common/logging.py) and controlled by environment variables in `.env` or `.env.{environment}`.

### Environment Variables

```ini
# src/common/config.py defaults
LOG_LEVEL=INFO                              # DEBUG, INFO, WARNING, ERROR
ACCESS_LOG_ENABLED=True                     # Enable request access logging
OTLP_LOGS_ENABLED=True                      # Export logs to OTLP endpoint
OTLP_ENDPOINT=http://localhost:4318         # Grafana Alloy endpoint
OTLP_SERVICE_NAME=api                       # Service identifier
OTLP_HEADERS=                                # Comma-separated: key=value,key2=value2
```

## Getting a Logger

### Standard Pattern

```python
from src.common.logging import get_logger

logger = get_logger(__name__)

# Structured logging with context
logger.info("user.created", user_id=user.id, email=user.email)
logger.warning("cache.miss", key="user:123", duration_ms=45)
logger.error("db.query.failed", table="users", query_time=1240, error=str(e))
```

### Using Module Const

```python
# At module top-level
logger: structlog.BoundLogger = structlog.get_logger("src.api.auth")
```

## Log Events and Structure

Each log event has:

- **Event name**: Dot-separated namespace (e.g., `app.startup`, `user.created`)
- **Log level**: DEBUG, INFO, WARNING, ERROR
- **Timestamp**: ISO 8601 with timezone
- **Context fields**: Key-value pairs for structured data

### Example Log Events

```python
# Application startup
logger.info(
    "app.startup",
    pid=12345,
    app_name="api",
    environment="prod",
    host="0.0.0.0",
    port=8000,
)

# Request processing
logger.info(
    "request.start",
    request_id="550e8400-e29b-41d4-a716-446655440000",
    method="POST",
    path="/api/auth/register",
    ip_address="192.168.1.100",
)

# Database operations
logger.info(
    "db.query",
    operation="SELECT",
    table="users",
    duration_ms=42,
    rows_affected=1,
)

# Authentication events
logger.info(
    "auth.login_success",
    user_id="550e8400-e29b-41d4-a716-446655440000",
    method="password",
    ip_address="192.168.1.100",
)

logger.warning(
    "auth.login_failed",
    username="john_doe",
    reason="invalid_password",
    ip_address="192.168.1.100",
    attempt=2,
)

# Cache operations
logger.info(
    "cache.hit",
    key="user:123:profile",
    duration_ms=1,
    size_bytes=1024,
)
```

## Development Environment Logging

In development (`env='dev'`):

- **Format**: Rich colored console output (human-readable)
- **Output**: stdout with syntax highlighting
- **Log Level**: Controlled by `LOG_LEVEL` (default: INFO)
- **Example Output**:
```
2026-02-27 10:45:23.123 [INFO] app.startup 
    pid=12345
    app_name='api'
    environment='dev'
    host='localhost'
    port=8000
    reload=True
```

## Production Environment Logging

In production (`env='prod'`):

- **Format**: JSON Lines (one JSON object per line)
- **Output**: stdout, typically collected by container orchestrator
- **OTLP Export**: Enabled by default, sends logs to Grafana Alloy
- **Example Output**:
```json
{"timestamp":"2026-02-27T10:45:23.123456Z","level":"info","event":"app.startup","pid":12345,"app_name":"api","environment":"prod"}
```

## OTLP Log Export

Logs can be exported to OpenTelemetry Collector / Grafana Alloy for centralized collection.

### Setup

1. **Enable OTLP in configuration**:
```ini
# .env.prod or .env.stage
OTLP_LOGS_ENABLED=True
OTLP_ENDPOINT=http://grafana-alloy:4318
OTLP_SERVICE_NAME=api
OTLP_HEADERS=Authorization=Bearer my-token
```

2. **Grafana Alloy receives logs on**:
   - HTTP endpoint: `http://localhost:4318/v1/logs`
   - Batch processor collects logs before export

3. **Logs flow**:
   Structured logging → OTLP Exporter → Alloy → Loki → Grafana Dashboard

### OTLP Handler Implementation

See [src/common/logging.py](../src/common/logging.py) `_build_otlp_handler()` for details.

Key components:
- `OTLPLogExporter`: HTTP exporter to OTLP endpoint
- `LoggerProvider`: Manages log processors
- `BatchLogRecordProcessor`: Batches logs before export
- `OtelLoggingHandler`: stdlib logging handler

## Access Log Middleware

HTTP request/response logging via `AccessLogMiddleware`:

**Configuration**:
```ini
ACCESS_LOG_ENABLED=True
```

**Logged for each request**:
- Request ID (trace ID)
- Method, path, query string
- Client IP address, user agent
- Response status code
- Response time (ms)
- Request/response body size
- User context (if authenticated)

**Example**:
```json
{
  "type": "access_log",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "method": "GET",
  "path": "/api/users/123",
  "status_code": 200,
  "duration_ms": 42,
  "client_ip": "192.168.1.100",
  "user_agent": "Mozilla/5.0...",
  "user_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

## Request Context & Tracing

Each request gets a unique identifier (`request_id`) that:

- Generated on request entry
- Propagated through all logs in that request
- Returned in response headers (`X-Request-ID`)
- Enables end-to-end request tracing

## Best Practices

1. **Use structured logging**: Always include context fields
   ```python
   # Good
   logger.info("user.created", user_id=user.id, email=user.email)
   
   # Avoid
   logger.info(f"User {user.id} created with email {user.email}")
   ```

2. **Use consistent event names**: Dot-separated, descriptive
   ```python
   # Good: module.entity.action
   logger.info("auth.user.login_success")
   logger.error("db.connection.failed")
   
   # Avoid: vague names
   logger.info("Something happened")
   ```

3. **Include relevant context**: User ID, request ID, duration, etc.
   ```python
   logger.info("db.query", user_id=user_id, table="orders", rows=15, ms=42)
   ```

4. **Log at appropriate levels**:
   - **DEBUG**: Detailed runtime information (variable values)
   - **INFO**: General events (login, cache hit, DB operation)
   - **WARNING**: Unexpected but recoverable situations
   - **ERROR**: Failures that impact functionality

5. **Sensitive data**: Never log passwords, tokens, API keys
   ```python
   # Good
   logger.info("login.attempt", user_id=user.id)
   
   # Bad
   logger.info("login.attempt", password="secret123")
   ```

## Testing Logging

In tests, capture logs to verify correct logging:

```python
import logging

def test_user_creation(caplog):
    with caplog.at_level(logging.INFO):
        create_user(username="john")
    
    assert "user.created" in caplog.text
    assert "john" in caplog.text
```

## Debugging Logs

**Increase log level for verbose output**:
```bash
LOG_LEVEL=DEBUG python app.py dev
```

**Filter logs by event name**:
```bash
python app.py dev 2>&1 | grep "auth\."
```

**View OTLP logs in Grafana Loki**:
```
{job="api"} | json | event =~ "auth.*"
```

---

**Related Documentation:**
- See [environment.md](environment.md) for environment-specific configuration
- See [monitoring.md](monitoring.md) for metrics and observability
