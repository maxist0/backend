# Logging

How the backend writes logs, which fields and events exist, and what must never be logged. Form: explanation with reference tables. The decision is [ADR 0004](adr/0004-logging-stdout-only.md); the rules for code are in [AGENTS.md → Security rules](../AGENTS.md#security-rules) and [Code style](../AGENTS.md#code-style).

## Pipeline

The application writes logs to **stdout only**. A collector (Alloy and Loki in the monitoring stack, or the Docker log driver) reads the container output; the application exports nothing itself.

`setup_logging(settings)` in `src/app/core/logging.py` configures structlog and the stdlib root logger with one handler, so records from structlog and from stdlib loggers (uvicorn, SQLAlchemy, taskiq) share one pipeline:

| `ENVIRONMENT` | Output |
|---|---|
| `dev` | coloured console renderer (colour only on a TTY; on Windows only when `colorama` is installed) |
| everything else | one JSON object per line |

Each process calls it once: the API in its lifespan, a worker process in the `WORKER_STARTUP` hook, the scheduler in `CLIENT_STARTUP`. Importing a module configures nothing.

Level and switches:

| Setting | Default | Effect |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Level of the root logger. An unknown name falls back to `INFO` |
| `ACCESS_LOG_ENABLED` | `true` | One `http.access` event per request |

Fixed adjustments: uvicorn's own handlers are removed and its access log is replaced by `http.access`; `sqlalchemy` and `python_multipart` stay at `WARNING`; the `taskiq` logger never goes below `INFO`, whatever `LOG_LEVEL` says, because taskiq logs whole task messages at DEBUG.

## Writing log events

```python
from app.core import get_logger

logger = get_logger(__name__)

logger.info("notes.word_count_stored", note_id=note_id, found=found)
```

- An event is a stable name, `area.action`, plus key-value fields. Do not write sentences or format values into the name.
- `user_id` is the only user context. Never log request bodies, query strings, `Authorization`, cookies, emails or usernames.
- Log an unexpected exception with `log_unexpected_error(logger, "area.event", exc)` or `error_type=type(exc).__name__`, never `error=str(exc)`: database and SMTP error text can contain user data. `log_unexpected_error` adds `error_type` and, for database errors, `db_error_type`, `sqlstate` and `constraint`, and no traceback for database errors.
- Do not log task arguments that hold anything but ids.

## Fields on every record

| Field | Source |
|---|---|
| `timestamp` | ISO 8601 |
| `level`, `logger` | stdlib level and logger name |
| `event` | the event name |
| `hostname`, `environment` | added to every record |
| `request_id` | bound by the request middleware for the request; the same value as the `X-Request-ID` header |
| `user_id` | bound by `CurrentUser` after authentication |

`http.access` carries `method`, `path` without the query string, `route` (the route template or `unmatched`), `status`, `duration_ms` and `client_ip` (from the ASGI scope; known gap: IPs are still logged, Roadmap Phase 3).

## Event names

The names that exist in the code (extracted from `src/app` on 2026-11-22; run `grep -rn 'logger\.' src/app` for the call sites):

| Area | Events |
|---|---|
| App | `app.startup`, `app.shutdown`, `app.shutdown_complete`, `app.unhandled_exception` |
| HTTP and health | `http.access`, `health.check_failed`, `rate_limit.fail_open` (at most once a minute per process, with `policy`, `error_type` and `suppressed`), `rate_limit.disabled` (startup, limits off outside `dev`/`unittest`) |
| Database and cache | `db.engine_creating`, `db.pool_init`, `db.pool_init_failed`, `db.pool_closed`, `cache.redis_connected`, `cache.connect_failed`, `cache.redis_closed` |
| Auth | `auth.user_registered`, `auth.register_conflict`, `auth.user_logged_in`, `auth.login_success`, `auth.login_failed`, `auth.login_attempt_unknown_user`, `auth.login_attempt_inactive_account`, `auth.login_attempt_locked_account`, `auth.password_confirmation_failed`, `auth.account_locked`, `auth.account_unlocked`, `auth.token_refreshed`, `auth.token_reuse_detected`, `auth.refresh_denied_account_disabled`, `auth.user_logged_out`, `auth.logout_all`, `auth.password_changed`, `auth.password_rehashed`, `auth.password_reset_completed`, `auth.password_reset_refused_account_blocked`, `auth.email_change_requested`, `auth.email_change_conflict`, `auth.email_changed`, `auth.email_verified`, `auth.email_token_refused_account_blocked`, `auth.verification_email_resent`, `auth.email_enqueue_failed`, `auth.cleanup_done`, `auth.optional_auth_failed`, `jwt.decode_error`, `jwt.invalid_token`, `jwt.token_expired` |
| Admin | `admin.login_succeeded`, `admin.login_failed`, `admin.login_rate_limited`, `admin.cross_site_request_rejected`, `admin.user_locked`, `admin.user_unlocked`, `admin.user_deactivated`, `admin.user_reactivated`, `admin.password_change_forced`, `admin.session_revoked` |
| Mail | `mail.configured`, `mail.not_configured`, `email.sent`, `email.not_sent` (with `reason`), `email.send_failed` |
| Tasks | `taskiq.heartbeat`, `taskiq.broker_unavailable`, `taskiq.broker_reconnected` |

## Reading logs in development

`run.py dev` prints the coloured console output of the API, the worker and the scheduler into one terminal. In a container, read `docker service logs` or `docker logs`; each line is JSON. Filter by `request_id` to follow one request across the API and, through the token id, the worker.

## Testing log output

`caplog` captures the records; `log_events(caplog, "event.name")` in `tests/helpers.py` returns the structlog event dicts, and `log_text(caplog)` returns everything as text, tracebacks included. Tests such as `tests/unit/modules/auth/test_logging_hygiene.py` and `tests/integration/modules/auth/test_logging_hygiene.py` assert that no username, email or token reaches a log line.

## Not implemented yet

- Trace and span ids in log records, OTLP export (Roadmap Phase 3; FastAPI's telemetry is switched off in `create_app()` because its log records would carry exception text).
- Hashing or dropping client IPs.
