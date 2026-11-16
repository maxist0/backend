# Observability review (application side), checked 2026-11-16

Scope: `src/common/{logging,metrics,middleware,exceptions,taskiq}.py`, `src/api/metrics/router.py`, `src/main.py`, log calls in `src/**`, `app.py`, `Dockerfile` healthcheck (read only). `deployment/**` was not reviewed (out of scope).
Verification status: everything marked "(verified)" was read in the code or fetched from a primary source. "(inferred)" means derived from library semantics and should get a regression test before relying on it. Nothing was executed (read-only task, no running stack).

Installed (uv.lock): fastapi 0.136.1, starlette 0.52.1, uvicorn 0.40.0, structlog 25.5.0, prometheus-client 0.24.1, opentelemetry-api/sdk 1.39.1, taskiq 0.12.2.

## Research results (primary sources)

| Question | Result | Source |
|---|---|---|
| Does FastAPI have native OpenTelemetry? | Yes (verified). `FastAPI(telemetry={...})` with keys `tracing`, `metrics`, `logs`, `operation_spans`, `exclude`, `auto_configure`, `tracer_provider`, `meter_provider`, `logger_provider`. Needs `fastapi[standard]` extra. Emits HTTP request span (`GET /items/{item_id}`), operation spans (dependencies, endpoint, serialization, each BackgroundTask), request count/duration/active-requests metrics, and logs for unhandled exceptions and validation failures (without invalid input). `auto_configure` is only on when `FASTAPI_OTEL_AUTO_CONFIGURE=true`; otherwise it uses the global providers you set up. Feed dates the feature to 0.142.0 (2026-09-29); 0.142.1/0.142.2 fixed auto-configure startup problems. The docs page does not mention `opentelemetry-instrumentation-fastapi` and warns against double exporters. | https://fastapi.tiangolo.com/advanced/opentelemetry/ , https://fastapi.tiangolo.com/release-notes/ (the exact 0.142.0 note text was not readable; version mapping is from the release feed, so treat "0.142.0" as likely, not proven) |
| Latest FastAPI | 0.163.1 (2026-11-15). Between 0.136.1 and 0.163.1 the release list marks "Breaking Changes" in 0.143.0, 0.145-0.151, 0.155, 0.160, 0.162.0, 0.162.6. The notes were not read, so the upgrade needs a changelog pass and a full test run. | release notes page |
| OTel Python | Latest 1.45.2 (0.66b2 contrib). `LoggingHandler` deprecated since 1.40 in favour of `opentelemetry-instrumentation-logging`; 1.44 moved the example. 1.45.0 has a breaking change for `Logger` subclasses (`enabled()`). Logs signal still underscore-private (`opentelemetry.sdk._logs`). | https://github.com/open-telemetry/opentelemetry-python/releases |
| HTTP semconv metrics | `http.server.request.duration`: stable, Histogram, unit `s`, buckets 0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75, 1, 2.5, 5, 7.5, 10. Required attrs `http.request.method`, `url.scheme`; conditionally `error.type`, `http.response.status_code`, `http.route`. `http.server.active_requests` and `http.server.request.body.size` are opt-in and "in development". Prometheus rendering of the stable metric: `http_server_request_duration_seconds_{bucket,sum,count}` with labels `http_request_method`, `http_route`, `http_response_status_code`, `url_scheme`, `error_type` (naming from the OTel-Prometheus compatibility rule, inferred). | https://opentelemetry.io/docs/specs/semconv/http/http-metrics/ |
| structlog + OTel correlation | Official docs show a custom processor reading `trace.get_current_span()` and adding `trace_id` (32 hex) and `span_id` (16 hex). | https://www.structlog.org/en/stable/frameworks.html |
| prometheus_client vs OTel metrics exporter | Not fetched. Judgement: keep `prometheus_client` for the pull model and app-level metrics (cheap, no SDK dependency, works with the existing Prometheus scrape); use OTel only for traces (and optionally logs). If FastAPI native `metrics` is enabled, its meter needs an exporter; the cleanest option is `opentelemetry-exporter-prometheus` reading into the same registry, or OTLP metrics to Alloy. Pick one source of truth for HTTP RED metrics (see Target design). Marked as design opinion. |

## Verdict on README known issues

| # | Verdict | Notes |
|---|---|---|
| 3 `KEYS rt:` | Confirmed (`src/api/metrics/router.py:31`, prefix is `"rt:"`, `src/common/constants.py:4`). Also O(N) blocking command on every scrape. | see OBS-10 |
| 4 `/metrics` skip | Confirmed (`middleware.py:100` compares to `/metrics`, router mounted at `/metrics/`). Redirect `/metrics`->`/metrics/` is skipped, the real scrape is counted. `/metrics/health` (Docker healthcheck every 30 s) is counted and access-logged as well. | OBS-09 |
| 12 Dockerfile healthcheck | Confirmed (`Dockerfile:49`, `/metrics/health` only exists when `METRICS_ENABLED`). Also hard-codes port 8000. | OBS-16 |

All three are still open. 25 further findings are NEW.

## Findings

Severity counts: Critical 1, High 9, Medium 14, Low 4 (28 total).

### OBS-01 | CRITICAL | gdpr (secret leak) | NEW
- File: `src/common/database.py:88` `logger.info("db.pool_init", url=settings.DATABASE_URL)`.
- Evidence: line 40 masks the password with `render_as_string(hide_password=True)`; line 88 logs the raw URL, so `postgresql+asyncpg://user:password@host/db` is written to stdout (and Loki, and OTLP) on every start.
- Impact: database credentials in logs of every environment. Violates AGENTS.md "never log credentials".
- Fix: drop the `url` field (or reuse the masked URL; better log only host, port, db name). Rotate DB password where this ran in prod. Add a test that asserts no log record contains the password.
- Effort S. Breaking: no.

### OBS-02 | HIGH | gdpr | NEW
- Files: `src/utils/email_tasks.py:32,34,57,59` (`to=to`, `username=username`), `src/common/mail.py:65` (`to=to`), `src/common/mail.py:41` (logs `MAIL_USERNAME`, `MAIL_FROM`, server).
- Impact: email addresses of data subjects in operational logs; SMTP login name in logs.
- Fix: log `task_id` plus a result (`email.sent`, template name, no recipient). If recipient correlation is needed use `user_id`: change the task signature to take `user_id` (tasks "receive IDs and primitives" per AGENTS.md) and resolve the address inside the task. Remove identity fields from `mail.setup`.
- Effort S-M. Breaking: task signatures change (queue must be drained on deploy).

### OBS-03 | HIGH | gdpr | NEW (README Observability section documents it as a feature)
- Files: `src/api/auth/dependencies.py:55,99` bind `username` into contextvars, so every later log line of the request carries the username; `src/api/auth/service.py:196,217,231,251,258,270,289,304`; `src/api/auth/router.py` (`auth.user_registered`, around line 86, logs username and duplicates the service log).
- Evidence: line 217 logs the submitted, unknown username (users often type an email or even a password there).
- Impact: violates AGENTS.md ("user context in logs is `user_id` only") and GDPR minimisation.
- Fix: bind `user_id` only. For unknown-user attempts log `auth.login_failed reason=unknown_user` without any identifier and count it in a metric (OBS-24); the audit table keeps the necessary data. Update README line 267.
- Effort S. Breaking: log schema only.

### OBS-04 | HIGH | gdpr | NEW (inferred, verify with a test)
- Files: `error=str(e)` together with `exc_info=True` in `src/api/auth/router.py` (100, 155, 199, 256, 290, 325, 365, 400), `src/api/user/router.py:70,111`, `src/common/exceptions.py:25`, `src/utils/jwt_utils.py:71,74`, `src/api/auth/dependencies.py:75`, `src/common/database.py:90`, `src/common/cache.py:40`.
- Evidence: SQLAlchemy `IntegrityError`/`DBAPIError` messages include the statement and `[parameters: (...)]` (emails, usernames, hashes). asyncpg errors include `DETAIL: Key (email)=(x@y) already exists`. PyJWT errors can echo token fragments.
- Impact: personal data and credential material leak through "safe" error logging, and the full traceback does too.
- Fix: one helper in `src/common/logging.py` that logs `error.type` and a sanitised summary (exception class and, for DB errors, `orig.sqlstate`/constraint name), never `str(exc)` for DB/JWT errors. Keep the traceback but add a structlog processor that scrubs SQLAlchemy parameter blocks, or set `hide_parameters=True` on `create_async_engine` (supported SQLAlchemy engine option, closes most of it at the source). Do not use `ExceptionDictTransformer` with default `show_locals=True` (locals would leak passwords); set `show_locals=False`.
- Effort M. Breaking: no.

### OBS-05 | HIGH | gdpr | NEW (README Phase 3 mentions part of it)
- File: `src/common/middleware.py:14-83`.
- Evidence: `path=full_path` includes the query string; `client_ip` is the leftmost `X-Forwarded-For` / `X-Real-IP` taken from any client without a trusted-proxy check (spoofable, so forged IPs go into logs); `referer` is logged raw (the SPA's own URL can contain `?token=` pages such as reset or verify); full UA string.
- Impact: query strings and referers can contain tokens; full IPs without a policy; attacker-controlled log content.
- Fix: log `url.path` using the route template (`http.route`) plus raw path only truncated, never query; drop `referer`; keep UA capped at 200 chars or drop; IP via one shared `client_ip()` honouring uvicorn proxy-headers with an explicit trusted CIDR (setting `TRUSTED_PROXIES`), then truncate (IPv4 /24, IPv6 /48) for operational logs. Decide the retention policy with the owner (open question).
- Effort M. Breaking: log schema only.

### OBS-06 | HIGH | bug | NEW (inferred from Starlette semantics, verify with a test)
- Files: `src/common/middleware.py:41-84` (AccessLog), `:232-268` (MetaData), `src/main.py:138-161` (order), `src/common/exceptions.py:25`.
- Evidence: registered with `add_middleware` last = outermost. Real order outermost -> innermost: ServerErrorMiddleware, AccessLog, Metrics, SecurityHeaders, GZip, [Cashews x2], CORS, MetaData, ExceptionMiddleware, router. `BaseHTTPMiddleware.call_next` runs the inner app in a separate anyio task, so contextvars bound in `MetaDataMiddleware` (request_id) or in dependencies (`user_id`) are invisible to the outer middlewares' code that runs after `call_next`. Result: the `http.access` event never carries `request_id` or `user_id`; the generic exception handler log (`app.unhandled_exception`, runs in ServerErrorMiddleware, outermost) has no `request_id` either.
- Impact: the primary correlation key is missing from the single most useful log line and from every 500 error log.
- Fix: replace by one pure ASGI `ObservabilityMiddleware` (outermost) that creates the request id, binds contextvars in the same task as the app (no task hop), logs access after the response completes, and catches unhandled exceptions (see OBS-07). Add a test asserting `request_id` in the access log and in the 500 log.
- Effort M. Breaking: no.

### OBS-07 | HIGH | bug | NEW
- Files: `src/main.py:129` (`add_exception_handler(Exception, ...)`), `src/common/middleware.py:98-167`.
- Evidence: a handler for bare `Exception` is installed on ServerErrorMiddleware, which sits outside all user middleware. The exception therefore propagates through Metrics/AccessLog first: `http_requests_total` and the duration histogram are only updated after `call_next` returns, so unhandled 500s are not counted in the request metrics at all (only `api_errors_total` increments). No access log is written for them. The 500 response is generated outside SecurityHeaders/CORS/MetaData, so it has no `X-Request-ID`, no CORS headers (the browser reports a CORS error instead of a 500) and no security headers. ServerErrorMiddleware re-raises after responding, so uvicorn additionally prints the traceback (duplicate, see OBS-15).
- Impact: error-rate SLI undercounts exactly the worst failures; RED and any availability SLO are wrong.
- Fix: the outermost pure ASGI middleware wraps the app in `try/except Exception`, records status 500 and `error.type`, logs once, sends the standard 500 envelope itself with the request id header, and does not re-raise. Keep the registered handler only as a fallback.
- Effort M. Breaking: no.

### OBS-08 | HIGH | bug | NEW
- File: `src/common/middleware.py:121,169-191`.
- Evidence: `Request` has no `route` attribute, so `hasattr(request, "route")` is always False; the label is always the raw path run through regexes. It is also computed before routing. Unmatched paths (404s from scanners, `/wp-admin/x.php`, random strings) each create new series in 3 histograms and 1 counter: unbounded cardinality (memory growth, scrape size). The regex order is also wrong: `/\d+` runs first so a UUID that starts with digits turns into `/{id}e4567-...` junk, and non-digit IDs (slugs, hex) are not normalised.
- Impact: DoS vector on Prometheus memory plus meaningless labels.
- Fix: read `scope.get("route").path` after the app finished (FastAPI sets `scope["route"]` when it matches); if absent use the constant `"__unmatched__"` (or omit `http.route` per semconv). Never put the raw path into a label. Series estimate after fix: ~30 routes x 5 methods x ~8 status classes -> under 1,500 series for the histogram (14 buckets) is about 21k bucket series; reduce by dropping `status_code` from the histogram (use status class on the counter only) or keep as semconv and accept.
- Effort S (inside the new middleware). Breaking: label names change.

### OBS-09 | MEDIUM | bug | README Known issue 4 confirmed
- File: `src/common/middleware.py:100`; also `/metrics/health` and docs endpoints.
- Fix: exclusion set matched on the resolved route/path with and without trailing slash: `/metrics`, `/metrics/`, `/health/*`. Excluded from metrics and access log (or log at DEBUG).
- Effort S. Breaking: no.

### OBS-10 | MEDIUM | bug | README Known issue 3 confirmed
- File: `src/api/metrics/router.py:28-34`.
- Evidence: `keys("rt:")` matches only the literal key `rt:`; `KEYS` is O(N) and blocks Redis; `except Exception: pass` hides failures; gauge refreshed only on scrape (stateful side effect inside a GET handler).
- Fix: remove `active_refresh_tokens` (README Phase 3 allows this) or compute from Postgres via a custom collector with a short cache; if kept in Redis use `SCAN MATCH rt:* COUNT 1000` in a background task, never in the scrape path. Log scrape failures at WARNING instead of swallowing.
- Effort S. Breaking: metric removed.

### OBS-11 | MEDIUM | bug | NEW
- File: `src/common/metrics.py:37-49`, `middleware.py:124-155`.
- Evidence: the two size histograms use prometheus_client default buckets (0.005 ... 10, designed for seconds), so every byte count lands in `+Inf`; they are useless. Response size is read from `content-length` or `response.body`; with GZip (inner) the length header is removed and a `_StreamingResponse` from `call_next` has no `body`, so it records 0. Request size uses `int(header)` which raises `ValueError` on a malformed header and turns it into a 500 (and into `api_errors_total`).
- Fix: drop both histograms (semconv marks body size opt-in/in development), or keep with byte buckets (e.g. 128, 1 KiB, 8 KiB, 64 KiB, 512 KiB, 4 MiB) and count bytes inside the ASGI `receive`/`send` wrappers. Recommended: drop.
- Effort S. Breaking: metrics removed.

### OBS-12 | MEDIUM | best-practice | NEW
- File: `src/common/metrics.py:20`.
- Evidence: a fresh `CollectorRegistry()` has none of the default collectors, so `process_cpu_seconds_total`, `process_resident_memory_bytes`, `process_open_fds`, `python_gc_*`, `python_info` are absent. USE metrics for the process are missing.
- Fix: register `ProcessCollector`, `PlatformCollector`, `GCCollector` on `REGISTRY`; add pool collectors (OBS-26). Document that `--workers N` breaks per-process gauges (no multiprocess mode configured; current `app.py` runs a single process).
- Effort S. Breaking: no.

### OBS-13 | HIGH | modernisation | README Phase 3 (not an existing known issue)
- Evidence: no `opentelemetry` instrumentation in `src/` (grep), no `trace_id`/`span_id` in log schema, no `traceparent` extraction or injection, no spans for SQLAlchemy/asyncpg/redis/taskiq.
- Impact: Tempo is deployed but empty; logs cannot be joined with traces.
- Fix: see Target design. Decision gate: FastAPI upgrade 0.136.1 -> >=0.142 (preferably latest 0.163.1 after reading the breaking-change notes) to use native telemetry, otherwise `opentelemetry-instrumentation-fastapi` (contrib, versioned 0.xb, so beta packaging) on 1.45.x. Either way add `opentelemetry-instrumentation-{sqlalchemy,redis,asyncpg?,httpx}` (asyncpg is redundant if SQLAlchemy is instrumented and gives duplicate spans; choose SQLAlchemy only, inferred), `OTEL_SEMCONV_STABILITY_OPT_IN=http` for the contrib route. Log correlation processor from the structlog docs.
- Effort L. Breaking: new dependencies and upgrade (needs user approval per boundaries).

### OBS-14 | HIGH | bug + modernisation | NEW (record-shape part inferred, verify with a test)
- File: `src/common/logging.py:56-97, 196-199, 239-245`.
- Evidence: (a) `opentelemetry.sdk._logs.LoggingHandler` is deprecated since 1.40 (pyproject allows >=1.31; the lock is 1.39.1, so the warning appears on upgrade). (b) The handler is attached to the root logger without the structlog `ProcessorFormatter`. structlog's `wrap_for_formatter` hands the stdlib logger the event dict as `record.msg`, so the OTLP body is the Python `repr` of a dict, not structured attributes; `request_id`, `user_id` become unusable. (c) stdout and OTLP both ship the same logs to Alloy: double ingestion unless one is disabled. (d) Resource has only `service.name`; no `service.version`, `service.instance.id`, `deployment.environment.name`. (e) Custom `OTLP_ENDPOINT`/`OTLP_HEADERS`/`OTLP_SERVICE_NAME` duplicate the standard `OTEL_EXPORTER_OTLP_*`/`OTEL_SERVICE_NAME` variables and the explicit `endpoint=` argument ignores them. (f) `src/common/logging.py:241` imports the private SDK module unguarded.
- Fix: decide one log path. Recommended: stdout JSON only (collector scrapes it; Loki labels stay low-cardinality), no in-process OTLP log export. If OTLP logs are wanted, use `opentelemetry-instrumentation-logging` and attach the handler with a formatter that emits the event dict as attributes. Remove the custom OTLP settings in favour of standard OTEL env vars.
- Effort M. Breaking: settings removed.

### OBS-15 | MEDIUM | bug | NEW (README Phase 3 says "keep uvicorn.error at INFO")
- File: `src/common/logging.py:210-222`.
- Evidence: for `uvicorn.access`, `uvicorn.error`, `sqlalchemy*`, `urllib3`, `python_multipart` the code sets level WARNING, clears handlers and sets `propagate=False`. A logger with no handlers anywhere in its chain falls back to `logging.lastResort` (plain text on stderr, WARNING+). So uvicorn errors (including the "Exception in ASGI application" traceback) and SQLAlchemy warnings bypass JSON formatting, `request_id`, and OTLP; INFO lifecycle lines (startup complete, shutting down) are lost. `log_config=None` in `app.py:88` is correct, but the intent was to route them through root.
- Fix: do not clear handlers or disable propagate; set levels only (`uvicorn.error` INFO, `uvicorn.access` WARNING or disabled because we log access ourselves, `sqlalchemy.engine` WARNING). Also stop the module-level variable `logger` in `setup_logging` shadowing the module logger.
- Effort S. Breaking: no.

### OBS-16 | MEDIUM | bug | README Known issue 12 confirmed + semantics NEW
- Files: `src/api/metrics/router.py:39-63`, `Dockerfile:48-49`.
- Evidence: one `/metrics/health` endpoint, tied to `METRICS_ENABLED`, returns "healthy" without touching Postgres or Redis; exposes `pod_name`/`node_name` unauthenticated; returns the success envelope; runs through the full middleware stack (access log, metrics, gzip, cache middlewares) every 30 s; `metrics_available: True` is a constant.
- Fix: `src/api/health/router.py` mounted at root (not behind `/api` prefix and not subject to the metrics flag): `GET /health/live` (always 200, no dependencies, no envelope, constant body), `GET /health/ready` (Postgres `SELECT 1` and Redis `PING` with 1-2 s timeouts via `asyncio.wait_for`, 503 with a list of failing checks, flag `app.state.ready`), `GET /health/startup` (200 after lifespan startup finished). Excluded from metrics, access log at DEBUG, tracing and rate limiting. Dockerfile `HEALTHCHECK` -> `/health/live` (deployment change, out of scope this round, but document it). Do not expose pod/node names.
- Effort M. Breaking: path change.

### OBS-17 | MEDIUM | best-practice | NEW (corrects README Phase 3 item)
- Evidence: README Phase 3 wants "readiness flips to 503 on SIGTERM". Uvicorn's shutdown order (inferred from its server code, verify): stop listening, wait for in-flight connections (graceful timeout), then run lifespan shutdown. After the listener closes, a readiness probe cannot be answered at all, and lifespan shutdown runs after the drain, so an in-process 503 flag set there is too late to help.
- Fix: rely on the orchestrator (Swarm `stop_grace_period`, start-first rolling updates, healthcheck-driven removal) and on `--timeout-graceful-shutdown`; make the readiness endpoint report failing dependencies only. In lifespan shutdown flush OTel (`TracerProvider.shutdown()`), close pools, log `app.shutdown` with duration. Update the README roadmap wording.
- Effort S. Breaking: no.

### OBS-18 | MEDIUM | bug | NEW
- Files: `src/common/middleware.py:247`, `src/common/responses.py:25,34-36`, `src/main.py:140-146`.
- Evidence: (a) `X-Request-ID` from the client is trusted unvalidated: arbitrary length/content is placed in logs and response headers (log forging, cardinality, header size). (b) `responses.py` reads `request.headers["X-Request-ID"]` (the inbound header) and otherwise creates a second `uuid4`, so when the client sent no id the envelope `metadata.request_id` differs from the logged id and the `X-Request-ID` response header. (c) CORS has no `expose_headers`, so browser JS cannot read `X-Request-ID` (needed for the frontend correlation). (d) Request id is not propagated into taskiq tasks.
- Fix: accept inbound id only if it matches `^[A-Za-z0-9._-]{8,64}$`, else generate (UUIDv4 or v7); store it in `scope["state"]["request_id"]`; `responses.py` reads that; `expose_headers=["X-Request-ID","traceparent"]`; pass `request_id` as a taskiq label and rebind in the worker. When tracing is on, also return `traceparent` is not required; `trace_id` goes into logs.
- Effort S-M. Breaking: no.

### OBS-19 | MEDIUM | bug | NEW (README Phase 3 only says "replace BaseHTTPMiddleware")
- File: `src/common/middleware.py` (all five classes: AccessLog, Metrics, CORSMiddleware(unused), SecurityHeaders, MetaData).
- Evidence of concrete pitfalls here: contextvar isolation (OBS-06); `call_next` returns when headers are ready, so durations (`X-Process-Time`, histogram, access log) are time-to-first-byte, wrong for streaming/SSE; response `.body` does not exist on streamed responses (OBS-11); `request.headers` parsing with `int()` can raise (OBS-11); BackgroundTasks run after the response and the BaseHTTPMiddleware task group waits for them in some Starlette versions (the old "background task blocks middleware" issue; not verified for 0.52); `clear_contextvars()` at the end is skipped on exceptions (no `try/finally`); `X-Process-Time` and `X-Request-ID` overlap with the exceptions path.
- Fix: pure ASGI middlewares (see Target design). `SecurityHeadersMiddleware` and the envelope-agnostic header setting also become pure ASGI (`send` wrapper that appends headers on `http.response.start`). `X-XSS-Protection` is a legacy header; consider `0` or removing.
- Effort M. Breaking: no (headers unchanged).

### OBS-20 | MEDIUM | bug | NEW
- Files: `src/common/taskiq.py:13-15`, `src/common/config.py:127-157`, `app.py:53-57`, `src/main.py` (no logging setup).
- Evidence: `setup_logging` is called only from `app.py` and from the import of `src/common/taskiq.py`. The API imports `taskiq` transitively (auth router -> email tasks), so the second call runs inside the API process with `os.getenv("ENVIRONMENT","dev")`. `get_settings` is `lru_cache`d per `env` argument, so `app.py --env prod` without an `ENVIRONMENT` variable gets a different `dev` Settings object, overwrites the global `settings`, and re-runs `setup_logging` with the dev renderer (console colours in a prod container). `uvicorn src.main:app` or `fastapi run` (Roadmap Phase 3 wants this) never configures logging.
- Fix: configure logging in exactly one place per process: a `create_app()`/lifespan hook for the API, a broker startup event for the worker; `get_settings()` without an env argument; no import-time side effects.
- Effort M. Breaking: no.

### OBS-21 | MEDIUM | best-practice | NEW
- Files: `src/common/taskiq.py`, `src/utils/*.py`, `src/common/config.py:45` (`TASKIQ_METRICS_PORT`, referenced only in config and compose).
- Evidence: no taskiq middlewares are registered: no task start/finish/failure log with `task_id`, `task_name`, duration and retry count; no metrics (the configured metrics port is unused); no worker health signal beyond the 5-minute `taskiq.heartbeat` log; `request_id` is not carried across. `retry_on_error=True` on the email tasks requires a retry middleware (`SimpleRetryMiddleware`, to verify in taskiq 0.12.2) and none is configured, so retries probably never happen. `send_templated_email` returns silently when mail is not configured, so the task succeeds without sending.
- Fix: a small taskiq middleware (`pre_execute` binds `task_id`, `task_name`, `request_id` label via `structlog.contextvars`; `post_execute`/`on_error` log `task.completed|task.failed` and observe `taskiq_task_duration_seconds{task_name,status}` and `taskiq_tasks_total{task_name,status}`); either taskiq's own Prometheus middleware (verify its existence and API in 0.12.2) or custom metrics on `TASKIQ_METRICS_PORT`; queue depth gauge from `LLEN` on the list broker key in a periodic worker task; add the retry middleware. Raise instead of skipping when mail is unconfigured outside dev.
- Effort M. Breaking: no. Note for security-auditor: tokens travel in task args through Redis db 1 in clear.

### OBS-22 | MEDIUM | gdpr | NEW
- Files: `src/api/auth/service.py:133,339` (audit table uses `request.client.host`), `src/common/middleware.py:14` (logs XFF).
- Evidence: two different IP derivations. Behind Traefik, `request.client.host` is the proxy address unless uvicorn trusts the proxy via `--forwarded-allow-ips`, so the audit table (security evidence) may store the proxy IP while access logs store a spoofable header value.
- Fix: one `client_ip(scope)` helper with a trusted proxy list setting (`FORWARDED_ALLOW_IPS`, uvicorn `proxy_headers`), used by audit and logs; operational logs store the truncated address, the audit table keeps the full IP only with a documented retention (separate legal basis, e.g. legitimate interest in security, with a purge job). Needs owner/DPO decision.
- Effort M. Breaking: no.

### OBS-23 | MEDIUM | gdpr | NEW
- File: `src/api/auth/dependencies.py:114` `logger.debug("auth.refresh_token_extracted", token_prefix=refresh_token[:8])`.
- Impact: a fragment of a bearer secret in logs (and a misleading event on every refresh when DEBUG is on). Also `cache.refresh_token_cached` logs `session_id` (acceptable, an ID).
- Fix: delete the log line.
- Effort S. Breaking: no.

### OBS-24 | MEDIUM | best-practice | NEW
- Evidence: security events exist as logs (`auth.account_locked`, `auth.token_reuse_detected`, `auth.login_failed`) but have no request context beyond ids (no truncated IP, no `request_id` in the access log, see OBS-06); `AuthenticationError` handler (`exceptions.py:78`) logs nothing, so denied access with a valid-looking token ("User not found", "deactivated", missing refresh cookie) leaves no log; no metric exists for auth outcomes, so brute-force/lockout alerting must parse logs. The `except Exception` blocks in routers (`auth/router.py`, `user/router.py`) swallow errors and log them a second time with `str(e)`; AGENTS.md forbids the pattern.
- Fix: counter `auth_events_total{event}` with a fixed enum (`login_success`, `login_failure`, `account_locked`, `token_refresh`, `token_reuse`, `password_reset`, `email_verified`, ...; about 10 series, no user labels). Log `auth.denied reason=<enum>` in the auth exception handler at INFO/WARNING. Remove router-level catch-alls so errors reach one place. Do not reduce any existing security log.
- Effort M. Breaking: no.

### OBS-25 | LOW | best-practice | NEW
- File: `src/common/logging.py`.
- Points: `ExceptionRenderer()` default renders the traceback as a plain string in JSON (fine, but a structured `dict_tracebacks` with `show_locals=False` is better for Loki/Tempo; see OBS-04); `ConsoleRenderer(colors=True)` unconditionally (prod uses JSON, but CI logs get ANSI); `_SHARED_PROCESSORS` is a mutated module global with an `__name__` check hack; static fields lack `service.name`, `service.version` (hostname is fine); messages that are not events: `mail.py:27,41,65` ("Mail settings not fully configured...", "Mail configuration set up successfully.", "FastMail not configured; skipping email.") break the `area.action` rule; `mail.py` does import-time setup (`fm = setup_mail()`).
- Fix: build the processor list inside a function; add `CallsiteParameterAdder` only in dev; rename events; add OTel correlation processor (OBS-13).
- Effort S. Breaking: no.

### OBS-26 | LOW | best-practice | NEW
- File: `src/common/metrics.py:75-87,139-158`, `src/api/auth/crud.py` (only the auth crud wraps `DatabaseTimer`; user and example crud do not).
- Evidence: `DatabaseTimer.__exit__` increments `api_errors_total{endpoint="database.<table>"}`, mixing a DB label into an endpoint label and counting expected exceptions (e.g. duplicate-key probes) as API errors; `table` and `operation` are free-form strings (bounded today only by discipline); no pool metrics (size, checked-out, overflow, wait time) although `DB_POOL_*` is tuned.
- Fix: delete `DatabaseTimer`/`record_*` helpers; use SQLAlchemy instrumentation (spans, `db.client.operation.duration` if enabled) plus a small collector or pool event listeners exposing `db_pool_connections{state=idle|in_use}`, `db_pool_overflow`, `db_pool_checkout_wait_seconds`. Cache hit/miss counters for the cashews route cache.
- Effort M. Breaking: metrics removed.

### OBS-27 | LOW | best-practice | NEW
- Files: `src/main.py:140-146` and `src/common/middleware.py:194-211`.
- Evidence: the custom `CORSMiddleware` class is dead code with a hard-coded origin; the live one hard-codes `http://localhost:5173` (AGENTS.md: every environment-dependent value comes from `Settings`) and `allow_headers=["*"]`. `__import__("time")` in `middleware.py:64,66` although `time` is imported. `lifespan` logs `cwd` and `app_description` (noise); `start_time = datetime.now()` is naive (also used in `responses.py`).
- Fix: delete the dead class; `CORS_ORIGINS` setting; clean up. (Middleware order and CORS changes are on the AGENTS.md "ask first" list.)
- Effort S. Breaking: no.

### OBS-28 | LOW | best-practice | NEW
- File: `pyproject.toml:24-28`.
- Evidence: both `opentelemetry-exporter-otlp` (pulls the gRPC exporter and grpcio) and `opentelemetry-exporter-otlp-proto-http` are listed with a `>=1.31.0` floor while 1.45.2 is current; the code only uses the HTTP exporter. `opentelemetry-api` and `-sdk` are hard dependencies although OTLP is optional.
- Fix: keep only `-http`, raise floors to the version actually tested after the upgrade, pin through `uv.lock`.
- Effort S. Breaking: no. (Dependency change needs approval.)

## Target design

### 1. Middleware set (pure ASGI, outermost first)
1. `ObservabilityMiddleware` (one class, no BaseHTTPMiddleware): validates or creates `request_id` (regex above), stores it in `scope["state"]`, binds `structlog.contextvars` (`request_id`, later `trace_id`, `span_id` through a processor, not a binding), wraps `send` to add `X-Request-ID` and `X-Process-Time` on `http.response.start` and to capture status and bytes, measures duration to end of body, catches unhandled exceptions (send the standard 500 envelope, count status 500 with `error.type`), records metrics and writes one `http.request` log event in `finally`, resolving `scope["route"]` for the route template. Skips `/health/*` and `/metrics*` for metrics and logs at DEBUG.
2. `SecurityHeadersMiddleware` (pure ASGI send-wrapper).
3. CORS (Starlette) with `expose_headers=["X-Request-ID","traceparent"]`, origins from `Settings`.
4. GZip, then cashews middlewares, then router. (Order change needs owner approval per AGENTS.md.)
Tracing: the FastAPI native span (or contrib ASGI instrumentation) wraps outside or inside step 1 consistently so the log processor sees the active span; when native telemetry is used, exclude health and metrics with `telemetry={"exclude": lambda scope: scope["path"].startswith(("/health", "/metrics"))}`.

### 2. Metrics (Prometheus via `prometheus_client`, own registry plus process/platform/gc collectors)
| Metric | Type | Labels | Series estimate |
|---|---|---|---|
| `http_server_request_duration_seconds` (semconv bucket list) | histogram | `http_request_method`, `http_route`, `http_response_status_code`, `error_type` (only on 5xx/exceptions) | routes (~30) x methods (~2 per route) x statuses (~6) x 15 series, ~5k worst case |
| `http_server_active_requests` | gauge | `http_request_method` | 7 |
| `auth_events_total` | counter | `event` (enum) | ~10 |
| `db_pool_connections` / `db_pool_overflow` / `db_pool_checkout_wait_seconds` | gauge / gauge / histogram | `state` | <10 |
| `cache_requests_total` | counter | `cache` (`route`,`refresh_token`), `result` (`hit`,`miss`) | 4 |
| `taskiq_tasks_total`, `taskiq_task_duration_seconds` | counter, histogram | `task_name`, `status` | tasks x 3 |
| `taskiq_queue_depth` | gauge | `queue` | 1-2 |
| `application_info`, process/python collectors | info/standard | | standard |
Removed: `http_requests_total`, `http_request_duration_seconds`, size histograms, `active_connections`, `active_refresh_tokens`, `database_operation*`, `api_errors_total`. RED comes from the one duration histogram (count = rate, status = errors). Requests-in-flight is `http_server_active_requests` (opt-in semconv, in development, so name is provisional). If native FastAPI `metrics` is enabled, do not also run a custom HTTP histogram: choose one source (preferred: keep the custom one so the endpoint, buckets and PII review stay under our control, set `telemetry={"metrics": False}` or only emit traces).

### 3. Log schema (JSON, one object per line on stdout)
`timestamp` (ISO, UTC), `level`, `event` (`area.action`), `logger`, `service.name`, `service.version`, `deployment.environment`, `hostname`, `request_id`, `trace_id`, `span_id` (when a span is active), `user_id` (UUID, when authenticated), plus event fields. Access event `http.request`: `http.request.method`, `http.route`, `http.response.status_code`, `duration_ms`, `response_bytes`, `client_ip_truncated`, `user_agent` (capped). Level by status: 5xx ERROR, 4xx INFO (WARNING for 401/403/429), others INFO. Forbidden fields: email, username, tokens, cookies, `Authorization`, query string, request or response bodies, full IP, raw `str(exc)` of DB/JWT errors. A CI test greps captured logs of the auth flow for the test user's email, username, password and token. Loki labels stay `service`, `environment`, `level`; ids stay in the body.
Retention policy (proposal for the owner/DPO): operational logs 14-30 days, security events (`auth.*`) and the DB audit table 6-12 months, each with a stated legal basis. Decision required.

### 4. Health endpoints
`/health/live` (no dependencies), `/health/ready` (Postgres, Redis, 503 plus failing check names), `/health/startup`. Root-level, no envelope, no auth, no pod/node names, not tied to `METRICS_ENABLED`, excluded from metrics, tracing and access log. Graceful shutdown per OBS-17.

### 5. Tracing
Native FastAPI telemetry (upgrade, needs approval) with `fastapi[standard]` extra, global `TracerProvider` created in the app factory with OTLP/HTTP span exporter using standard `OTEL_*` env vars, `Resource` with `service.name`, `service.version`, `deployment.environment.name`; head sampling `ParentBased(TraceIdRatio(OTEL_TRACES_SAMPLER_ARG))` (default 0.1 in prod, 1.0 in dev) with tail sampling of errors and slow requests in Alloy (deployment, later round). Instrumentations: SQLAlchemy (not asyncpg in addition), redis, httpx (when used), taskiq middleware that extracts/injects `traceparent` in task labels and creates a consumer span. W3C `traceparent` extracted by the instrumentation and returned to browsers only via `X-Request-ID`/logs. Do not record SQL parameters, request bodies or query strings as span attributes (`hide_parameters=True`, no `url.query`). `OTEL_SEMCONV_STABILITY_OPT_IN=http` if the contrib package is used. Expected span volume at R rps and sampling p: R x p x ~8 spans per request (HTTP, ~3 deps/serialization, ~3 DB/Redis); example 50 rps x 0.1 x 8 = 40 spans/s (needs the owner's volume budget).
Logs stay on stdout only; no in-process OTLP log export (OBS-14).

## Proposed implementation order
1. Hot fixes, no design needed (same day): OBS-01, OBS-23, OBS-02, OBS-03, OBS-04 (`hide_parameters=True`, helper for safe errors), OBS-15, OBS-09 (path skip), then OBS-10 (remove the gauge). Add the log-scrubbing test.
2. Logging setup single source (OBS-20, OBS-25), settings cleanup (remove custom OTLP settings, OBS-14).
3. Pure ASGI `ObservabilityMiddleware` with request id, access log, metrics and 500 handling (OBS-06, 07, 08, 11, 12, 18, 19, 05, 22 partly); migrate `SecurityHeaders`; fix CORS expose headers and origins (OBS-27; needs approval for order/CORS change).
4. Health endpoints (OBS-16, 17); Dockerfile healthcheck change is a follow-up in the deployment round.
5. Auth event metrics and denied-access log (OBS-24); taskiq middleware, retries, metrics (OBS-21); DB pool and cache metrics, delete `DatabaseTimer` (OBS-26).
6. Approval gate: FastAPI upgrade (read 0.143-0.162 breaking changes) and OTel upgrade to 1.45.x; enable tracing, correlation processor, SQLAlchemy/redis instrumentation, sampling (OBS-13, 28).
7. SLOs and alert rules (login, refresh, `GET /user/me`) after metrics are live; they live in `deployment/` (next round).

## Open questions for the owner (needed before step 3/6)
1. Retention and legal basis per log class; IP truncation policy; keep full IP in the audit table?
2. Trusted proxy CIDR for `X-Forwarded-For` in Swarm/Traefik.
3. Permission to upgrade FastAPI (native telemetry) and OTel, and to change middleware order/CORS.
4. Trace volume budget and sampling ratio; request rate expectation.
5. Prometheus-only HTTP metrics (recommended) or OTel metrics through Alloy?
