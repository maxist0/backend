# API Backend Boilerplate

A production-grade, framework-complete REST API skeleton built on **FastAPI**, **PostgreSQL**, **Redis** and **Python 3.14**. It ships authentication, sessions, background jobs, caching, structured logging, metrics and a Docker Swarm deployment, and deliberately **no business features**. Add a domain module, not plumbing.

> **Status:** hardening in progress. The foundation is sound (async everywhere, Argon2id, PyJWT, refresh-token rotation, Testcontainers), but several production gaps are documented in [Roadmap](#roadmap) and [Known issues](#known-issues). Phase 0 and Phase 0b of the November 2026 review (security fixes and the follow-up from the re-audit) are implemented but not yet committed (223 tests pass; ruff and mypy are clean). The consolidated review and its re-prioritised plan are in [documentation/review-2026-11/README.md](documentation/review-2026-11/README.md). Treat `main` as a boilerplate under review, not as a release.

Contributor and AI-agent conventions live in [AGENTS.md](AGENTS.md). Read it before changing code.

---

## Contents

1. [What you get](#what-you-get)
2. [Quick start](#quick-start)
3. [Project layout](#project-layout)
4. [System design](#system-design)
5. [Design decisions](#design-decisions)
6. [Security posture](#security-posture)
7. [Configuration](#configuration)
8. [Deployment](#deployment)
9. [Roadmap](#roadmap)
10. [Known issues](#known-issues)
11. [Open decisions](#open-decisions)
12. [Standards and references](#standards-and-references)

---

## What you get

| Area | Implemented today |
|---|---|
| Framework | FastAPI 0.136 on Starlette, Pydantic v2, fully async (SQLAlchemy 2 async + asyncpg, redis asyncio) |
| Auth | Register, login (OAuth2 password form), logout, logout-all, refresh with rotation and reuse detection (family revocation), email verification, password reset, account lockout, audit log |
| Sessions | Opaque refresh tokens (SHA-256 hashed, stored only in Postgres), JWT access tokens (15 min, HS256, `sid` claim = session family id) |
| Users | `GET` and `PATCH /api/v1/users/me` (profile), `PUT /api/v1/users/me/password`, `PUT /api/v1/users/me/email` (pending change, completed by `POST /api/v1/auth/email/verify`) |
| API contract | Everything under `/api/v1`, plain typed responses, RFC 9457 `application/problem+json` errors with a stable `code` ([ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md), frontend view: [backend-contract.md](../frontend/documentation/backend-contract.md)) |
| Data | PostgreSQL, Alembic migrations, UUID primary keys, timezone-aware timestamps |
| Caching | `@cached(ttl, key)` and `invalidate(pattern)` on Redis (`cashews`, JSON values), `RedisDep` for direct Redis access |
| Background jobs | `taskiq` with Redis broker, result backend, cron scheduler, transactional email tasks |
| Observability | `structlog` JSON logs on stdout with request IDs, Prometheus metrics at `/metrics`, `/health` and `/health/ready`, Grafana/Loki/Tempo/Alloy stack |
| Admin | `sqladmin` panel at `/admin`: read-only users, sessions and audit log; every change is an audited action through the auth services; opt-in outside `dev` via `ADMIN_ENABLED` |
| Testing | pytest integration suite against real Postgres and Redis via Testcontainers |
| Packaging | `uv` lockfile, multi-stage Dockerfile, non-root container, Docker Swarm stack with Traefik |

---

## Quick start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) 0.13 or newer (manages Python 3.14 for you)
- Docker Desktop or Docker Engine (for Postgres, Redis and the test suite)
- Git

### 1. Install

```bash
git clone <repo-url> backend
cd backend
uv python install 3.14
uv sync
```

`uv sync` creates `.venv` and installs the locked dependencies including the `dev` group (ruff, mypy).

### 2. Start infrastructure

```bash
docker run -d --name api-postgres -e POSTGRES_PASSWORD=admin -e POSTGRES_DB=api -p 5432:5432 postgres:18
docker run -d --name api-redis -p 6379:6379 redis:7-alpine
```

A `compose.dev.yaml` for this step is on the roadmap (Phase 0).

### 3. Configure

Copy the template and adjust if your ports differ:

```bash
cp .env .env.local
```

`.env.local` is only read when you start with `python app.py dev`. All other modes expect variables injected by the runtime (Docker, Swarm, CI). The defaults in `.env.local` match the containers above (`postgres:admin@localhost:5432/api`, Redis on `6379`).

### 4. Migrate and run

```bash
uv run alembic upgrade head
uv run python run.py dev
```

The dev launcher also starts a `taskiq` worker and the `taskiq` scheduler as child processes, so emails, scheduled jobs and retries run locally; it stops them on exit. Failed tasks are retried only while the scheduler runs ([ADR 0009](documentation/adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md)).

**Windows:** psycopg cannot use Windows' default `ProactorEventLoop`. `run.py` starts uvicorn on a `SelectorEventLoop`. Instead of the `taskiq worker` CLI it runs `python -m app.worker`, one worker process on a `SelectorEventLoop`, so `TASKIQ_WORKERS` has no effect there. `fastapi run` works on Windows only with `--workers 2` or more.

| URL | Purpose |
|---|---|
| http://localhost:8000/docs | Swagger UI (disabled in `prod`) |
| http://localhost:8000/redoc | ReDoc |
| http://localhost:8000/metrics | Prometheus metrics |
| http://localhost:8000/health | Liveness check |
| http://localhost:8000/health/ready | Readiness check (PostgreSQL and Redis) |
| http://localhost:8000/admin | Admin panel (mounted by default only in `dev`; `admin` / `admin` by default, change it) |

### 5. Verify

```bash
# Lint, format check, types
uvx ruff@0.15.0 check src tests
uvx ruff@0.15.0 format --check src tests
uv run mypy src

# Tests (needs a running Docker daemon; the suite starts Postgres and Redis containers)
uv run pytest
uv run pytest tests/auth/test_auth.py::TestLogin -x     # one class
uv run pytest -k "refresh" -x                           # by keyword
```

---

## Project layout

```
backend/
├── app.py                     # CLI launcher: python app.py [dev|unittest] [--host] [--port]
├── pyproject.toml             # PEP 621 metadata, dependencies, pytest + coverage config
├── uv.lock                    # Locked dependency graph (commit it)
├── Dockerfile                 # Multi-stage build, non-root runtime
├── alembic.ini / alembic/     # Migrations (async env.py)
├── src/
│   ├── main.py                # FastAPI app factory, lifespan, middleware, routers
│   ├── admin.py               # sqladmin views and auth backend
│   ├── api/                   # Feature modules (one folder per bounded context)
│   │   ├── auth/              # router, service, crud, models, schemas, dependencies, exceptions
│   │   ├── user/              # profile endpoints (re-uses auth models)
│   │   └── exampleRoute/      # empty template module (to be replaced, see Roadmap)
│   ├── common/                # Cross-cutting infrastructure
│   │   ├── config.py          # pydantic-settings Settings, environments
│   │   ├── database.py        # async engine, session factory, get_db
│   │   ├── dependencies.py    # DbSession = Annotated[AsyncSession, Depends(get_db)]
│   │   ├── cache.py           # Redis client (RedisDep), @cached and invalidate
│   │   ├── taskiq.py          # broker, scheduler, task registration
│   │   ├── mail.py            # fastapi-mail configuration
│   │   ├── logging.py         # structlog + stdlib bridge, stdout only
│   │   ├── db_errors.py       # constraint_name, sqlstate, find_db_error for DB errors
│   │   ├── middleware.py      # one ASGI middleware: request id, access log, HTTP metrics, security headers
│   │   ├── health.py          # /health, /health/ready, /metrics
│   │   ├── responses.py       # response envelope helpers
│   │   ├── schemas.py         # envelope models, ErrorCode enum
│   │   ├── exceptions.py      # global exception handlers
│   │   ├── models.py          # SQLAlchemy DeclarativeBase
│   │   └── constants.py       # app-wide constants
│   ├── templates/email/       # Jinja2 email templates
│   └── utils/                 # jwt_utils, email_tasks, scheduled tasks
├── tests/                     # pytest; conftest spins up Postgres + Redis containers
├── deployment/docker/
│   ├── api/                   # Swarm stack: Traefik, Postgres, Redis, API x3, taskiq worker/scheduler/dashboard
│   ├── monitoring/            # Prometheus, Grafana, Loki, Tempo, Alloy, exporters
│   └── network/               # standalone Traefik for local compose
├── documentation/             # Topic docs (partly stale, see Roadmap Phase 7); review-2026-11/ holds the review backlog
└── implementation-plan/       # Superseded by AGENTS.md and this README
```

---

## System design

### Runtime topology

```
                         ┌──────────────┐
   Internet ──HTTPS──▶   │   Traefik    │  TLS termination, routing, rate limiting (planned)
                         └──────┬───────┘
                                │ HTTP
            ┌───────────────────┼────────────────────┐
            ▼                   ▼                    ▼
     ┌────────────┐      ┌────────────┐       ┌────────────┐
     │  api (x3)  │      │  api       │       │  api       │   uvicorn, one process each
     └─────┬──────┘      └─────┬──────┘       └─────┬──────┘
           │  asyncpg          │  redis-py          │
           ▼                   ▼                    ▼
     ┌────────────┐      ┌────────────┐       ┌──────────────────┐
     │ PostgreSQL │      │   Redis    │◀──────│ taskiq worker x2 │  emails, cron jobs
     │  (users,   │      │ db0 cache  │       │ taskiq scheduler │
     │  sessions, │      │ db1 queue  │       └──────────────────┘
     │  tokens,   │      │ db2 results│
     │  audit)    │      └────────────┘
     └────────────┘
           │ metrics /metrics, logs stdout (JSON)
           ▼
     Prometheus ── Grafana ── Loki ── Tempo ── Alloy (OTLP receiver)
```

### Request lifecycle

Middleware is applied in `src/app/main.py`; the last one added runs first. Effective order for a request:

1. Starlette's `ServerErrorMiddleware` (last-resort 500 for a failure in the middleware itself).
2. `CORSMiddleware` with `CORS_ORIGINS` from `Settings`. It answers CORS preflights itself, so preflights skip the next step.
3. `RequestMiddleware` (`src/app/core/middleware.py`, pure ASGI): validates or generates the request id, binds it to the `structlog` context and returns it as `X-Request-ID`; sets `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` and, under `API_PREFIX`, `Cache-Control: no-store` unless the route set one; records `http_requests_total` and `http_request_duration_seconds`; emits one `http.access` event. An unhandled exception is logged with `log_unexpected_error` and answered here with the generic 500 problem, so the 500 carries the same headers.
4. Router → dependencies (`DbSession`, `CurrentUser`) → handler → service → crud.
5. Exception handlers translate `AppError`, `RequestValidationError` and Starlette `HTTPException` into RFC 9457 problem responses (see [Error handling](#error-handling)).

### Module pattern

Every feature lives in `src/api/<feature>/` and is split by responsibility:

| File | Responsibility | May import |
|---|---|---|
| `router.py` | HTTP only: parse request, call service, shape response, set status code | service, schemas, dependencies |
| `service.py` | Business rules, orchestration, audit, side effects (tasks, cache invalidation) | crud, models, utils, other services |
| `crud.py` | Database and Redis access, no business rules | models, schemas (for cache payloads) |
| `models.py` | SQLAlchemy ORM models | `src.common.models.Base` |
| `schemas.py` | Pydantic request/response models | nothing project-specific |
| `dependencies.py` | FastAPI dependencies (auth, lookups that raise 404) | crud, service |
| `exceptions.py` | Named exceptions for this feature | `src.common.exceptions` |

The dependency arrow points inward: `router → service → crud → models`. Routers never touch the session directly, services never build `JSONResponse` objects.

### Configuration and environments

`src/common/config.py` defines one `Settings` class (pydantic-settings). `ENVIRONMENT` selects behaviour. Outside `dev` and `unittest` a startup guard refuses to boot with default or weak secrets (see [Configuration](#configuration)):

| Value | Docs | Log format | `.env.local` | Notes |
|---|---|---|---|---|
| `dev` | on | coloured console | read | taskiq worker and scheduler auto-started by `run.py` |
| `unittest` | on | JSON | ignored | used by `tests/conftest.py` |
| `test`, `stage` | on | JSON | ignored | runtime injects variables |
| `prod` | off | JSON | ignored | admin panel mounted only with `ADMIN_ENABLED=true`; startup guard active |

### Authentication design

```
register ──▶ user + argon2id credential + VERIFY_EMAIL token ──▶ email task
login    ──▶ lock check, admission limiter (PASSWORD_HASH_CONCURRENCY), lock re-check,
             verify password (Argon2 in thread pool; dummy verify for unknown or inactive users),
             success = conditional atomic UPDATE (refused once locked) ──▶ JWT access (15 min, HS256; claims exp, iat, nbf, sub, jti, type=access, sid)
                           └▶ opaque refresh (32 B urlsafe)
                                ├─ sha256 → sessions table (family_id, device, ip), Postgres only
                                └─ cookie refresh_token, HttpOnly, SameSite=Lax, Path=<API_PREFIX>/auth (/api/v1/auth)
refresh  ──▶ atomic UPDATE ... RETURNING on the session row (not revoked, not expired)
         ├─ success: issue new pair in the same family
         ├─ token already rotated or revoked: reuse detected → revoke the whole family
         │   (strict: two concurrent refreshes of the same token count as reuse)
         └─ user inactive or admin-locked: refresh denied, family revoked
            (a timed lock does not affect refresh)
logout       ──▶ revoke the session if it is active (an already rotated token is reuse:
                 family revoked, 401, cookie cleared)
logout-all   ──▶ revoke all sessions of the user, clear the cookie (bearer required)
password reset  ──▶ revoke all sessions, invalidate every open email token
password change ──▶ revoke all sessions except the caller's (`sid`), invalidate every open email token
```

**Pending email tokens.** A pending email change or verification ends with every event that should end an intruder's hold on the account: password change, password reset, admin lock and admin deactivation invalidate all open email tokens of the user, in the same commit as the session revocation. `POST /auth/email/verify` refuses a token of an inactive or admin-locked account with the same 400 `invalid_token` as an unknown one, and spends it, so lifting the block later does not revive a link that may be in the wrong hands.

**Password confirmation counts like a login.** A wrong current password on `PUT /users/me/password` or `PUT /users/me/email` increments the same failure counter (`LOGIN_MAX_ATTEMPTS`) and sets the same timed lock as failed logins, with `LOGIN_FAILED` and `ACCOUNT_LOCKED` audit rows (detail "Password confirmation"). As after a login lockout, sessions stay valid; while the lock lasts, both endpoints answer 400 `wrong_password` even for the correct password, and the password login answers 401. A correct password resets the counter, as a successful login does.

Access tokens are stateless and remain valid until expiry (up to 15 minutes), with one exception: requests with an access token of an admin-locked user get 401. Revoking a session therefore ends refresh, not already issued access tokens. Every auth event is written to `auth_audit_log`.

**Two kinds of lock.**

| | Timed lock | Admin lock |
|---|---|---|
| Set by | `LOGIN_MAX_ATTEMPTS` (5) consecutive failed logins | admin panel action "Lock selected" |
| `users` columns | `is_locked`, `locked_until` set (`LOGIN_LOCKOUT_SECONDS`, 900 s) | `is_locked`, `locked_until` NULL (indefinite) |
| Password login | blocked | blocked |
| Existing sessions, access tokens, refresh | stay valid | access tokens get 401 at once; refresh is denied and the family revoked |
| Sessions | untouched | all revoked by the admin action |
| Ends | on its own when `locked_until` has passed | admin unlocks |

A timed lock blocks only password login. Anyone who knows a username could otherwise trigger it and log that user out, so it must not end sessions (source: `User.is_admin_locked()` in `src/api/auth/models.py`, used by `src/api/auth/dependencies.py` and the refresh gate in `src/api/auth/service.py`).

**Login burst protection.** Each process admits at most `PASSWORD_HASH_CONCURRENCY` password evaluations at a time. A login re-reads the lock state after it is admitted, so guesses queued behind the one that locked the account are refused without an Argon2 evaluation. A successful login is one conditional atomic `UPDATE`: no login succeeds once a lock is committed. With N worker processes the bound is N × `PASSWORD_HASH_CONCURRENCY`. Rate limiting ([D4](#open-decisions)) is still the main control and is still open.

**Serialisation.** Refresh and the bulk revocations (`revoke_user_sessions`: logout-all, password reset and change, admin lock and deactivation; `revoke_family`: reuse detection, denied refresh) first take a row lock on the user (`SELECT ... FOR NO KEY UPDATE` on `users`, `lock_user` in `src/api/auth/crud.py`). They queue per user, so a revocation always sees the successor session of a parallel refresh. The global lock order is `email_tokens` → `users` → `sessions`.

**Admin panel.** The views (users, sessions, audit log) are read-only: sqladmin answers create, edit and delete with 403. Changes are actions that call auth service functions (`admin_*` in `modules/auth/service.py`): lock (indefinite, ends all sessions), unlock (also resets failed logins), deactivate (ends all sessions), reactivate, force password change (sets `must_change`, not enforced yet, Known issue 16) and revoke selected sessions. Each user or session is one transaction in the lock order above, and each writes an audit row whose `detail` names the admin (`... by admin <ADMIN_USERNAME>`). A user or session that no longer exists is skipped. There is no create or delete; account deletion is a later GDPR flow.

**Duplicate registrations.** Two parallel registrations with the same username or email are decided by the unique constraints. The loser gets 409 with the same message as the pre-check, never 500.

### Data model

| Table | Purpose | Notable columns |
|---|---|---|
| `users` | identity | `username`, `email` (unique), `is_active`, `is_email_verified`, `is_locked`, `failed_login_attempts`, `locked_until` |
| `password_credentials` | one row per user | `password_hash` (argon2id), `must_change` (set by the admin, not enforced yet) |
| `sessions` | refresh tokens | `token_hash` (unique), `family_id`, `is_revoked`, `revoke_reason`, `expires_at`, device and IP metadata |
| `email_tokens` | verification, change, reset | `purpose`, `token_hash`, `new_email`, `expires_at`, `used_at`, `invalidated_at` |
| `auth_audit_log` | append-only security log | `event_type`, `ip_address`, `user_agent`, `detail` |

All primary keys are UUIDv4 generated in the application. Timestamps are `timestamptz` in UTC.

### Caching and background work

- **Cache:** `@cached(ttl, key)` and `invalidate(pattern)` from `app.core` cache return values of service functions in Redis ([ADR 0003](documentation/adr/0003-caching-via-cached-decorator.md)). Values are stored as JSON, never pickled, so a value in Redis cannot run code in the app. There is no HTTP response cache.
- **Jobs:** `taskiq` with taskiq-redis' `RedisStreamBroker` on Redis db 1 (stream `taskiq:tasks`, consumer group `taskiq`), results in db 2 for one hour, stored as JSON ([ADR 0009](documentation/adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md)). Delivery is at least once: a worker acknowledges a message after the task and deletes the entry together with the acknowledgement, and a message unacknowledged for 10 minutes goes to another worker, so tasks must be idempotent. Tasks are declared with `@task(task_name=...)` in a module's `tasks.py`. A task labelled `retry_on_error=True` that raises `TransientTaskError` is retried `TASKIQ_RETRY_COUNT` times, `TASKIQ_RETRY_DELAY_SECONDS` apart. The retry is stored as a JSON schedule (keys `taskiq_retry:*` in db 1), and the scheduler sends it. The scheduler reads those retries and the `schedule=[{"cron": ...}]` labels. Run exactly one scheduler. Worker and scheduler are separate Swarm services. While Redis is unreachable, or the consumer group has vanished (`FLUSHALL`), the worker logs `taskiq.broker_unavailable` with the consecutive failures and the outage duration every few seconds, then `taskiq.broker_reconnected`, and resumes on its own. Wrong Redis credentials stop it.

### Observability

- **Logs:** `structlog` with contextvars. Every request has `request_id`; authenticated requests add `user_id` only. Logs contain no usernames, emails, query strings, `Referer` or token prefixes, and the database password is masked. `dev` renders coloured console output, every other environment JSON lines; everything goes to stdout and a collector reads the container output ([ADR 0004](documentation/adr/0004-logging-stdout-only.md)). `http.access` carries `request_id`, method, path without query string, route template, status, duration and client IP (from the ASGI scope).
- **Metrics:** prometheus_client's default registry (with the process and GC collectors) at `GET /metrics`: `http_requests_total{method,route,status}`, `http_request_duration_seconds{method,route}` (5 ms to 10 s buckets), `app_info{name,version,environment}`, and the auth counters `auth_login_failures_total{reason}`, `auth_lockouts_total`, `auth_refresh_token_reuse_total`. The `route` label is the route template (for example `/api/v1/users/me`) or `unmatched`, so arbitrary paths cannot create label values; `/metrics` itself is not counted. One process per container is assumed; several uvicorn workers in one container would need prometheus_client's multiprocess mode.
- **Worker metrics:** taskiq's `PrometheusMiddleware` serves `GET /metrics` on `TASKIQ_METRICS_HOST`:`TASKIQ_METRICS_PORT` (loopback by default) in the worker only: `received_tasks_total`, `success_tasks_total`, `found_errors_total` (every failed attempt, retried ones included) and `saved_results_total`, all with a `task_name` label, plus the `execution_time` histogram. The worker processes write to prometheus_client's multiprocess directory (`PROMETHEUS_MULTIPROC_DIR`, default `<tmp>/taskiq-metrics`, set by `app.worker` before anything imports prometheus_client). The first process to bind the port serves the sum of all processes. The directory must start empty, which a fresh container does. There is no queue-length gauge; use redis_exporter's stream checks for the backlog.
- **Health:** `GET /health` (liveness, no I/O) and `GET /health/ready` (PostgreSQL `SELECT 1` and Redis `PING`, each bounded by `READINESS_TIMEOUT_SECONDS`; 503 with the failed check, never error text).
- **Traces:** not yet wired (Tempo is deployed, the application does not emit spans; see Roadmap Phase 3).

### Error handling

Success responses are plain typed models without an envelope. Errors are RFC 9457 `application/problem+json` ([ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md)):

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The request contains invalid input.",
  "code": "validation_error",
  "request_id": "0161c42e-d89a-4892-af69-4e8934eb2fd9",
  "errors": [{ "field": "username", "code": "string_too_short", "message": "String should have at least 3 characters" }]
}
```

- `errors[]` appears only on 422. Body fields have no prefix; other sources are prefixed (`query.x`, `header.x`, `cookie.x`, `path.x`).
- Every 401 carries `WWW-Authenticate: Bearer`. A 202 or 204 has no body and no `Content-Type`.
- A 500 has no `request_id` yet (known limitation, fixed in S1.4).
- Services raise `AppError` subclasses (`src/app/core/errors.py`); the codes of the auth module are in `src/app/modules/auth/errors.py`. The complete code list with the meaning for clients is in the frontend's [backend contract](../frontend/documentation/backend-contract.md#error-codes).

---

## Design decisions

| Decision | Choice | Why | Alternatives considered |
|---|---|---|---|
| Concurrency | async end to end, no sync DB or Redis calls | FastAPI and uvicorn are ASGI; one blocking call stalls the event loop | sync SQLAlchemy with threadpool |
| Package manager | `uv` with committed `uv.lock` | reproducible installs, fast CI and Docker builds, PEP 735 groups | pip-tools, Poetry |
| Python | 3.14 target | bugfix phase, full wheel coverage, deferred annotations, required by Starlette 1.x and SQLAlchemy 2.1 floors (3.11+) | 3.13 (security-only from 2026-10), 3.15 (too new for all wheels) |
| Password hashing | `argon2-cffi` Argon2id, parameters from `Settings` (defaults t=3, m=64 MiB, p=4), run in a thread pool behind a concurrency limiter, rehash on login | OWASP first choice; defaults exceed the OWASP minimum (m=19 MiB, t=2); the limiter bounds memory use | bcrypt (72-byte limit), passlib (unmaintained) |
| JWT | `PyJWT[crypto]`, HS256 pinned in code (not configurable) | maintained, what the FastAPI tutorial uses; a fixed algorithm list prevents algorithm confusion | python-jose (critical unpatched CVE in 2026) |
| Refresh tokens | opaque, hashed at rest, rotated on every use, family-based reuse detection, HttpOnly cookie | OAuth 2.1 / RFC 9700 guidance for public clients; stolen tokens are detectable | JWT refresh tokens (not revocable without a denylist) |
| Session store | Postgres only; refresh is one atomic `UPDATE ... RETURNING` | a single gate removes the race between a Redis mirror and the database; the audit trail survives Redis restarts | Redis fast path (removed in Phase 0), Redis only |
| ORM | SQLAlchemy 2 async, `expire_on_commit=False`, explicit `select()` | typed, mature, Alembic autogenerate | SQLModel, raw asyncpg |
| Migrations | Alembic with async `env.py`, run as a separate step before rollout | never migrate at import time; CI can run `alembic check` | app-startup migrations |
| Primary keys | UUID generated client-side | safe to expose, mergeable across environments | bigint identity (planned evaluation of UUIDv7, Postgres 18) |
| Background jobs | `taskiq` + Redis | async-native, FastAPI dependency integration, cron via labels | Celery (no async tasks), arq (maintenance only) |
| Cache | `cashews` behind `@cached`/`invalidate`, JSON values | maintained; caches return values, not responses | fastapi-cache2 (dormant since 2024), route cache with ETag middleware (removed) |
| Logging | `structlog` over stdlib, JSON in prod | third-party loggers are captured by the stdlib bridge; machine-readable | loguru, stdlib only |
| Metrics | `prometheus_client` default registry | pull model, Grafana-native, process and GC metrics included | push to OTLP only |
| Email | `fastapi-mail` + Jinja2 templates, sent from tasks | never block a request on SMTP | synchronous send in handler |
| Admin | `sqladmin` | zero-code CRUD for support staff | custom admin, none |
| Testing | integration tests against real Postgres and Redis (Testcontainers) | catches SQL, migration and cookie behaviour that mocks hide | mocked repositories |
| Deployment | Docker Swarm with Traefik | matches current infrastructure; Kubernetes kept open | Kubernetes, bare uvicorn |
| Scope | no business features | the repo is a boilerplate; domain modules are added per project | sample "items" CRUD |

---

## Security posture

Implemented:

- Argon2id password hashing (parameters from `Settings`, thread pool with concurrency limiter, rehash on login); dummy verify for unknown and inactive users to reduce timing differences. Every login refusal (unknown user, wrong password, locked or inactive account) is the same 401 `invalid_credentials`; a locked account waits a calibrated time instead of running Argon2, so the response time does not reveal the lock
- Account lockout after 5 failed logins for 15 minutes with an atomic counter, audit-logged. This timed lock blocks only password login; existing sessions, access tokens and refresh stay valid, so nobody can log another user out by guessing. The admin lock is indefinite: it rejects access tokens at once, ends sessions on refresh, and the admin action "Lock selected" revokes all sessions
- Login burst protection: at most `PASSWORD_HASH_CONCURRENCY` password evaluations in flight per process, lock re-checked after admission, success as a conditional atomic `UPDATE` (N processes allow N times that bound). Not a substitute for rate limiting ([D4](#open-decisions), open)
- Atomic refresh gate in Postgres; reuse of a rotated token revokes the whole family; refresh is denied and the family revoked for inactive or admin-locked users. Refresh and bulk revocations are serialised per user by a row lock (`FOR NO KEY UPDATE` on `users`; lock order `email_tokens` → `users` → `sessions`)
- Parallel duplicate registrations return 409 with the normal message, never 500
- Error logs carry `error_type` and, for database errors, `db_error_type`, `sqlstate` and `constraint`, but no exception text (database error text can contain user data); tracebacks of database errors are not logged (`log_unexpected_error` in `src/common/logging.py`)
- Metrics label `method` is limited to the standard HTTP methods plus `other`
- Session revocation on password reset (all) and password change (all except the caller's); `POST /auth/logout-all`
- JWT: HS256 pinned in code; required claims `exp`, `iat`, `nbf`, `sub`, `jti`; `type=access` checked; `sub` must be a UUID; admin-locked users get 401 on access tokens
- Tokens (refresh, email) stored only as SHA-256 hashes; email tokens are atomic and single use
- Email-enumeration-safe `password/forgot`
- `EmailStr`-based email validation that rejects whitespace and control characters; `User-Agent` truncated to 512 and `X-Device-*` headers to 255 characters
- Startup guard outside `dev` and `unittest`: `JWT_SECRET_KEY` must not be the default and must be at least 32 characters; admin secrets are checked only when the admin panel is enabled
- Admin panel opt-in (`ADMIN_ENABLED`, default on only in `dev`), read-only views, actions only through audited service functions (one transaction per row, admin name and client IP in the audit row), actions use `?pks=`, fixed redirect, constant-time credential comparison, `admin.login_succeeded`/`admin.login_failed` logs with the client IP (never the typed username); CSRF: `SameSite=Strict` session cookie (8 h, `Secure` outside `dev`/`unittest`) plus a fail-safe `Sec-Fetch-Site` check: state-changing admin requests (unsafe methods and actions) without `Sec-Fetch-Site: same-origin` get 403 ([OWASP CSRF cheat sheet, Fetch Metadata headers](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html))
- Logs without usernames, emails, query strings, `Referer` or token prefixes; database password masked in logs; SQLAlchemy engine uses `hide_parameters`
- Security headers on every response, the 500 included (`X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`), `Cache-Control: no-store` under `API_PREFIX`; validated request id; client IP only from the ASGI scope, with uvicorn's proxy headers trusted only from `FORWARDED_ALLOW_IPS`
- CORS origins from `CORS_ORIGINS`; `*` is refused because credentials are allowed
- OpenAPI docs disabled in `prod`
- Non-root container user, multi-stage image
- Security upgrades of dependencies (starlette 1.10, python-multipart 0.0.33, pyjwt 2.15.1, sqladmin 0.32, aiosmtplib 5.1.3, cryptography 50, certifi, pydantic-settings 2.16, idna) and transitive security floors in `[tool.uv] constraint-dependencies`

Not yet implemented (tracked in the [Roadmap](#roadmap) and the [review backlog](documentation/review-2026-11/README.md)):

- `Secure` flag and `__Host-` prefix on the refresh cookie
- Rate limiting (per IP and per account), request body size limit, request timeout
- JWT `iss` and `aud` claims
- HSTS (owned by the TLS-terminating proxy), CSP `frame-ancestors 'none'`
- Authentication on `/metrics`
- Hashing or dropping client IPs in logs (IPs are still logged)
- Dependency and image scanning, SBOM, signed images

---

## Configuration

All settings are environment variables read by `Settings` (`src/common/config.py`). The most important ones:

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `dev` | `dev`, `unittest`, `test`, `stage`, `prod` |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/api` | asyncpg driver required at runtime |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `20` | per process |
| `CACHE_HOST` / `CACHE_PORT` / `CACHE_DB` / `CACHE_PASSWORD` | `localhost` / `6379` / `0` / empty | Redis for `RedisDep` and `@cached` (refresh tokens are not stored in Redis) |
| `TASKIQ_BROKER_URL` / `TASKIQ_RESULT_BACKEND_URL` | Redis db 1 / db 2 | Task stream and retry schedules / task results (1 h TTL). The broker's Redis must not evict keys (`noeviction`) |
| `TASKIQ_WORKERS` | `2` | `1..64`. Processes of `taskiq worker` started by `run.py dev` (on Windows always one) |
| `TASKIQ_METRICS_HOST` / `TASKIQ_METRICS_PORT` | `127.0.0.1` / `9000` | IP address or hostname / `1..65535`. Prometheus endpoint of the worker. Loopback by default; a container sets `0.0.0.0` so the scraper on the monitoring network reaches it |
| `TASKIQ_RETRY_COUNT` / `TASKIQ_RETRY_DELAY_SECONDS` | `3` / `60` | `0..10` / `30..3600`. Retries after the first run of a task that raises `TransientTaskError`, and the wait before each (plus up to 1 s jitter and up to the scheduler's update interval) |
| `JWT_SECRET_KEY` | placeholder | **Secret.** Startup guard outside `dev`/`unittest`: must not be the default and must be at least 32 characters. Use a 256-bit random value. The JWT algorithm is HS256, pinned in code |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | `15` / `30` | |
| `ADMIN_ENABLED` | unset | Unset means enabled only in `dev`. Set `true` to mount the admin panel elsewhere; the Swarm stack sets `false` |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_SECRET_KEY` | `admin` / `admin` / placeholder | **Secrets** (`ADMIN_PASSWORD`, `ADMIN_SECRET_KEY`). The startup guard rejects the defaults and values shorter than 32 characters outside `dev`/`unittest`, but only when the admin panel is enabled |
| `ARGON2_TIME_COST` / `ARGON2_MEMORY_KIB` / `ARGON2_PARALLELISM` | `3` / `65536` / `4` | Argon2id parameters. Minimums `≥2` / `≥19456` / `≥1` (OWASP minimum), validated in every environment; lower values stop startup |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_LOCKOUT_SECONDS` | `5` / `900` | Consecutive failed password logins that set a timed lock, and its length. Bounds `1..100` (NIST SP 800-63B-4 3.2.2) / `1..86400` |
| `PASSWORD_HASH_CONCURRENCY` | `4` | `≥1`, validated in every environment. Maximum concurrent Argon2 operations and concurrent login credential checks per process (the bound is N × this value with N processes); each Argon2 operation needs `ARGON2_MEMORY_KIB` of RAM |
| `MAIL_*` | unset | mail is skipped when incomplete |
| `LOG_LEVEL` / `ACCESS_LOG_ENABLED` / `METRICS_ENABLED` | `INFO` / `true` / `true` | Logs go to stdout only; `METRICS_ENABLED` mounts `/metrics` |
| `READINESS_TIMEOUT_SECONDS` | `2` | Per check of `GET /health/ready` (`>0`, `≤30`) |
| `API_PREFIX` | `/api/v1` | Path prefix of every module router (the API version) and of the refresh cookie path (`<API_PREFIX>/auth`). Empty, or segments of `[A-Za-z0-9._~-]` without a trailing slash. `/`, `/health`, `/health/ready` and `/metrics` stay unprefixed |
| `FRONTEND_URL` | `http://localhost:3000` | used in email links |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | JSON list of browser origins allowed with credentials. `*` and values with a path are refused in every environment |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Comma-separated proxy addresses whose `X-Forwarded-For`/`-Proto` uvicorn trusts (`run.py` enables proxy headers). Set it to the reverse proxy's addresses, or every client IP is the proxy's |

Secrets must never be committed. `.env` in the repository is a template with empty values; `.env.local` is git-ignored. When the startup guard fails, the application refuses to start and names the offending variables, not their values.

---

## Deployment

### Image

```bash
docker build -t api:latest .
```

The Dockerfile installs dependencies with `uv sync --frozen --no-dev` in a builder stage and copies only `.venv` and the source into a `python:3.13-slim` runtime as user `appuser`. Entry point is `python app.py`; `ENVIRONMENT` must be injected. A hardened variant (Python 3.14, digest-pinned base, cache mounts, bytecode compilation, dev dependencies excluded, `fastapi run`) is Roadmap Phase 6.

### Docker Swarm stack

`deployment/docker/api/docker-compose.yml` defines Traefik, Postgres 16, Redis 7, a one-shot `migrate` service, three API replicas with rolling updates, two taskiq workers, one scheduler and the taskiq dashboard.

```bash
docker swarm init                      # once
docker network create -d overlay --attachable monitoring
docker build -t api:latest .
docker stack deploy -c deployment/docker/api/docker-compose.yml --with-registry-auth prod
docker stack rm prod
```

Export `JWT_SECRET_KEY` in the shell that runs `docker stack deploy`. The compose file uses `${JWT_SECRET_KEY:?...}` for `api`, `taskiq-worker` and `taskiq-scheduler`, so the deploy fails when the value is missing or empty. `docker stack deploy` does not read `.env`. Set `DB_PASSWORD` and `REDIS_PASSWORD` the same way; their compose fallbacks are development values. The stack sets `ADMIN_ENABLED=false`, so the admin panel is not mounted.

### Monitoring stack

`deployment/docker/monitoring/docker-compose.yml` runs Prometheus, Grafana (dashboards for Postgres, Redis, Docker, Loki and taskiq are provisioned), Loki, Tempo, Alloy, cAdvisor and the Postgres and Redis exporters. See `deployment/docker/monitoring/README.md`.

---

## Roadmap

This is the original implementation plan. The re-prioritised plan from the November 2026 review (Phase 0 to 6) is in [documentation/review-2026-11/README.md](documentation/review-2026-11/README.md); where the two orderings differ, the review plan wins. Phases below are ordered by risk reduction per effort. Each item is a checkbox so progress is visible in the repo; acceptance criteria are in italics.

### Phase 0: Repository and tooling hygiene

- [ ] Add `[tool.ruff]` with an explicit strict rule set (`E,W,F,I,B,UP,S,ASYNC,N,D,ANN,RUF,PL,PT,SIM,T20,ERA,TRY,FAST,C4,DTZ,EM,LOG,G,PTH,RET,ARG,TC,BLE,FBT,A,ISC,PIE,PERF,FURB,SLF,TID,ICN,INP,PGH`), per-file ignores for `tests/**` and `alembic/**`, `target-version = "py314"`. *Baseline of 818 findings reduced to 0 or explicitly ignored with a reason.*
- [ ] Enable `mypy --strict` in `pyproject.toml`; add `ty` as an advisory check. *`uv run mypy src` exits 0.*
- [ ] Move pytest, pytest-asyncio, pytest-cov, httpx, testcontainers, mypy, ruff out of `[project.dependencies]` into PEP 735 groups `test` and `lint`; `dev` includes both. Remove the duplicate `[project.optional-dependencies].dev`. *Production image contains no test tooling.*
- [ ] Fix packaging: switch to the `uv_build` backend with an `src` layout, or rename the package; `version` in `pyproject.toml` and `APP_VERSION` come from one source. *`uv build` produces a wheel that imports cleanly.*
- [ ] Add `.python-version` (3.14), `requires-python = ">=3.14"`, and upgrade the base image to `python:3.14-slim`.
- [ ] Add `.env.example` (replace the committed `.env`), ignore `htmlcov/`, `api.egg-info/`, `.coverage`.
- [ ] Add `compose.dev.yaml` with Postgres 18 and Redis 7 for local development.
- [ ] Add a task runner (`justfile` or `Makefile`): `install`, `dev`, `test`, `lint`, `typecheck`, `migrate`, `build`.
- [ ] Add `pre-commit` (or `prek`) with ruff, mypy, gitleaks, `uv lock --check`.
- [ ] Replace `exampleRoute/` with a real, tested template module (`src/api/_template/`) or remove it; fix the `curd.py` typo. Collapse `user/` re-export shims or give the module real content.
- [ ] Repair the local virtualenv (`uv sync --reinstall`) and document it.

### Phase 1: Security hardening

- [x] Startup guard: refuse to boot outside `dev`/`unittest` when `JWT_SECRET_KEY` (or, with the admin panel enabled, `ADMIN_PASSWORD`/`ADMIN_SECRET_KEY`) carries a default; require `len(JWT_SECRET_KEY) >= 32` (and, with the admin panel enabled, 32 characters for both admin secrets). Partly: `DATABASE_URL` defaults are not checked.
- [ ] Pin the JWT algorithm allowlist in code; add `iss`, `aud`, `typ=access`; verify `type` on decode; reject tokens without `jti`. *RFC 8725 checklist satisfied.* Partly done: algorithm pinned, claims `exp`/`iat`/`nbf`/`sub`/`jti` required, `type=access` verified, `sub` must be a UUID. `iss` and `aud` are still missing.
- [ ] Refresh cookie: `Secure=True` outside `dev`, `SameSite=Strict` where the frontend allows it, `__Host-` prefix, explicit `max_age`.
- [x] Revoke all sessions on password reset and password change; add `POST /auth/logout-all`. (Password change keeps the caller's session.)
- [x] Complete the email-change flow: `PUT /users/me/email` creates a pending change, `POST /auth/email/verify` consumes the `CHANGE_EMAIL` token (no separate confirm endpoint), dedicated template (`confirm_email_change.html`), `EMAIL_CHANGED` audit event; confirming invalidates open password-reset tokens. The `FRONTEND_URL` in the reset link is tracked in the email-links item below (Known issue 7).
- [ ] Rate limiting with `limits` + Redis: per IP on `/auth/*`, per account on login; return `429` with `Retry-After`. Decouple lockout from DoS by lockout-by-IP-window instead of hard account locks. Partly mitigated in Phase 0b: the timed account lock blocks only password login and leaves existing sessions valid, and a per-process admission limiter bounds login bursts; both remain no substitute for rate limits.
- [x] Uniform credential error (`Invalid username or password`, code `invalid_credentials`) for locked, inactive and unknown accounts; the real reason is logged and audited server-side only.
- [ ] Security headers: add HSTS (behind TLS), `Cache-Control: no-store` on authenticated responses, `Content-Security-Policy: frame-ancestors 'none'`; drop `X-XSS-Protection`. Partly done: `X-XSS-Protection` is gone and every response under `API_PREFIX` gets `Cache-Control: no-store`; HSTS belongs to the TLS proxy; the CSP is still open.
- [x] Trusted proxies: run uvicorn with `--proxy-headers --forwarded-allow-ips=<traefik>`; remove manual `X-Forwarded-For` parsing; use the same client IP source for access logs and audit logs. (`run.py` with `FORWARDED_ALLOW_IPS`; access and audit logs read the ASGI scope.)
- [ ] CORS origins, methods and headers from `Settings` (`CORS_ORIGINS` list); delete the dead custom `CORSMiddleware` class. Partly done: origins come from `CORS_ORIGINS` (`*` refused); methods and headers are still `*`.
- [ ] Admin panel: Argon2 hash for the admin password with constant-time compare, `ADMIN_ENABLED` flag defaulting to `false` in `prod`, hide `token_hash` columns. Partly done: `ADMIN_ENABLED` (default on only in `dev`) and constant-time compare of the admin credentials exist. Still open: Argon2 hash for the admin password, and `token_hash` is still shown in the sessions detail view.
- [ ] Protect `/metrics` (network policy or bearer token); remove the `KEYS` scan. Partly done: the `active_refresh_tokens` gauge and its `KEYS` scan are removed; `/metrics` is still unauthenticated.
- [ ] Request limits: `max_body_size`, per-request timeout, `strict_content_type` kept on.
- [x] Pydantic request models with `extra="forbid"`; separate create/update/response schemas (OWASP API3). Every request model of the `auth` and `users` modules forbids extra fields (`schemas.py`); the login form is OAuth2 form data.
- [ ] `SECURITY.md` with disclosure process and supported versions.
- [ ] Email links to the frontend routes (requested by the frontend, [task 2](../frontend/documentation/backend-contract.md#backend-task-2-email-links-to-the-frontend-routes)): absolute URLs from `FRONTEND_URL` to `/user/email-bestaetigen?token=`, `/user/passwort-zuruecksetzen?token=` and `/user/einladung?token=` (`/en/user/…` when the user's locale is known); URL-encoded token; fix the `FRONTEND_URL` default (frontend dev server runs on 5173). Fixes Known issue 7.
- [ ] `POST /auth/password/forgot`: return 200 even when the email task fails (log internally) and equalise response times for known and unknown addresses (requested by the frontend, [task 3](../frontend/documentation/backend-contract.md#backend-task-3-distinct-error-codes-and-a-consistent-forgot-password-answer); OWASP Forgot Password Cheat Sheet).

### Phase 2: API contract

- [x] API versioning: all module routers are mounted under `API_PREFIX` (`/api/v1`); `/`, `/health`, `/health/ready` and `/metrics` stay unversioned. Old paths return 404 (no deprecation window; the frontend migrates right after S1).
- [ ] Align the Swarm stack's Traefik router with `/api/v1`: `api-prod-strip` still strips `/api`, which would hand the app `/v1/...` (see [backend task 4](../frontend/documentation/backend-contract.md#backend-task-4-refresh-cookie-path-behind-the-api-prefix)).
- [x] Error format decided (D1, [ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md)) and one consistent handler in `src/app/core/errors.py`: validation, auth, named domain errors, unhandled 500 (no internals leaked), rendered by the request middleware with `X-Request-ID`, `request_id` and the security and CORS headers.
- [x] Named exceptions in services instead of `(None, "message")` tuples; routers do not catch `Exception`. A module's `errors.py` subclasses the `AppError` family from `app.core`, which maps each class to a status and a `code`.
- [x] `response_model` (or a return annotation) on every module route; no `top_level_fields`; FastAPI serialises Pydantic directly. Swagger login works through `LoginResponse`, a `TokenResponse` plus the user. The Prometheus exposition at `/metrics` is plain text.
- [ ] Pagination helper: cursor-based for lists, bounded `limit` (default 20, max 100), stable ordering.
- [ ] Optional `Idempotency-Key` support for POST (Redis-backed, 24 h TTL, 409 on payload mismatch) to match the frontend blueprint.
- [ ] `ETag`/`If-Match` on mutable resources (412 on mismatch).
- [ ] Health endpoints: `GET /health/live` (no dependencies), `GET /health/ready` (DB `SELECT 1`, Redis `PING`, 503 on failure or during shutdown), `GET /health/startup`. Update the Dockerfile `HEALTHCHECK` and Swarm probes. Partly done: `GET /health` (liveness) and `GET /health/ready` (503 on a failed check) exist and the Dockerfile `HEALTHCHECK` uses `/health`; still open: 503 during shutdown, the Swarm probes.
- [ ] OpenAPI: `summary`, `description`, `responses` per route; export the schema in CI and lint it (Spectral or vacuum); diff against the previous release (oasdiff). Partly done: every module route has a summary, a docstring description and `application/problem+json` error responses listing the possible `code` values (`tests/unit/core/test_openapi_contract.py`). Still open: response examples, the CI export, lint and diff.
- [ ] Timezone-aware timestamps everywhere (`datetime.now(timezone.utc)`), RFC 3339 output.
- [ ] Invitation endpoints `POST /auth/invitations/lookup` and `POST /auth/invitations/accept` (requested by the frontend, [task 1](../frontend/documentation/backend-contract.md#backend-task-1-invitation-endpoints-new); the frontend page is built against this contract): tokens only in request bodies, stored as SHA-256 hashes, single use and expiring; the email is bound to the invitation; the accepted user is created with `is_email_verified: true` and joined to the workspace in one transaction; rate limiting, no enumeration, audit events.
- [ ] Distinct error codes for the token endpoints: `token_invalid`, `token_expired`, `token_used` on `password/reset` and `email/verify`, `email_already_verified` on `email/verify/resend` (requested by the frontend, [task 3](../frontend/documentation/backend-contract.md#backend-task-3-distinct-error-codes-and-a-consistent-forgot-password-answer)). Partly done: `email_already_verified` ships; all token failures are still one `invalid_token`.

### Phase 3: Reliability and observability

- [x] Replace `BaseHTTPMiddleware` implementations with pure ASGI middleware (streaming-safe, lower overhead). One `RequestMiddleware` ([ADR 0005](documentation/adr/0005-single-asgi-request-middleware.md)).
- [ ] Tracing: enable FastAPI native OpenTelemetry (`FastAPI(telemetry=...)`, 0.142+) plus SQLAlchemy, asyncpg, redis and httpx instrumentation; export OTLP to Alloy → Tempo; propagate W3C `traceparent`; add `trace_id`/`span_id` to log records.
- [ ] Logs: redact PII (no emails, usernames or query strings in `http.access`; hash or drop IPs per policy); use `opentelemetry-instrumentation-logging` instead of the deprecated `LoggingHandler` path; keep `uvicorn.error` at INFO.
- [ ] Metrics: align with OTel HTTP semantic conventions (`http.server.request.duration` in seconds with the standard buckets, `http.route` label); add cache hit/miss, task queue depth, DB pool usage; drop the broken `active_refresh_tokens` gauge (done in review Phase 0) or compute it from Postgres.
- [ ] Graceful shutdown: `--timeout-graceful-shutdown`, readiness flips to 503 on SIGTERM, lifespan closes pools and flushes OTel; Swarm `stop_grace_period`.
- [ ] Settings as an explicit dependency (`get_settings()` cached) instead of a mutable module global; no import-time side effects in `taskiq.py` and `mail.py`.
- [ ] `fastapi run` / `fastapi dev` via `[tool.fastapi] entrypoint`; `app.py` becomes a thin wrapper or is removed.
- [ ] Alerting rules in Prometheus (error rate, p95 latency, pool exhaustion, brute force) and a provisioned API dashboard.

### Phase 4: Data layer

- [ ] Unit-of-work: crud functions stop committing; the service (or a `get_db` dependency with `begin()`) owns the transaction. Registration becomes atomic.
- [ ] Base model mixins: `created_at`, `updated_at` (server defaults), optional `version` for optimistic locking, optional soft-delete with partial indexes.
- [ ] Evaluate UUIDv7 primary keys (Postgres 18 `uuidv7()`); document the decision.
- [ ] Session cleanup job: purge expired sessions and tokens nightly (taskiq cron).
- [ ] SQLAlchemy naming convention for constraints; `alembic check` in CI; migration review checklist (expand/contract, `CONCURRENTLY`).
- [ ] Upgrade path to SQLAlchemy 2.1 (`sqlalchemy[asyncio]`, cancellation-safe pool).

### Phase 5: Supply chain and CI/CD

- [ ] CI pipeline (platform to be decided): `uv lock --check`, ruff, mypy, pytest with coverage gate (branch coverage, threshold defined once), `uv audit`/`pip-audit`, gitleaks, semgrep, Docker build, Trivy and Grype scan, SBOM (CycloneDX) attached to the image, SLSA provenance.
- [ ] Dependabot or Renovate for `uv.lock`, Docker base images and CI actions (pin actions by SHA).
- [ ] Conventional Commits enforced; automated changelog and semantic version tags.
- [ ] Mutation testing (mutmut) on `auth/service.py` nightly; Schemathesis run against the OpenAPI schema.

### Phase 6: Deployment hardening

- [ ] Dockerfile: `COPY --from=ghcr.io/astral-sh/uv:<pinned>`, cache mounts, `UV_COMPILE_BYTECODE=1 UV_NO_DEV=1 UV_PYTHON_DOWNLOADS=0`, two-step `uv sync --frozen --no-install-project` then `--locked`, digest-pinned `python:3.14-slim`, `tini` or exec-form `fastapi run`, read-only root FS with `tmpfs`, healthcheck without external deps.
- [ ] Swarm stack: Docker secrets for all credentials, `migrate` with `restart_policy: condition: none`, Traefik v3 with TLS, HTTP→HTTPS redirect, rate-limit middleware, no public `/docs`, `/openapi.json`, `/metrics`, `/admin`; resource limits reviewed; `stop_grace_period`.
- [ ] Monitoring stack: pinned image tags, Grafana admin password from secret, cAdvisor without `privileged` where possible, Prometheus scraping Swarm service DNS instead of `host.docker.internal`.
- [ ] Kubernetes manifests or Helm chart as an optional second target (probes, PDB, HPA).

### Phase 7: Documentation and compliance

- [ ] Rewrite `documentation/*.md` to match the code or fold them into `docs/` with ADRs; delete descriptions of code that does not exist.
- [ ] `CHANGELOG.md`, `CODEOWNERS`, `CONTRIBUTING.md`, `LICENSE` (see Open decisions).
- [ ] Compliance appendix: EU Cyber Resilience Act (SBOM, vulnerability handling, 24h/72h reporting duties active since 2026-09-11), NIS2 supply-chain clauses, ISO 27001:2022 controls A.8.15/8.16/8.25/8.28/8.29, GDPR log retention policy.
- [ ] Load test baseline (k6) and documented SLOs.

---

## Known issues

Verified during the November 2026 review and updated after the review's Phase 0 and 0b (uncommitted). The Roadmap phase of each open issue is given in brackets. The authoritative backlog, including findings that are not listed here, is [documentation/review-2026-11/README.md](documentation/review-2026-11/README.md).

Fixed in Phase 0:

- ~~1~~ The test harness now uses the Redis Testcontainer.
- ~~3~~ `/metrics/` no longer scans Redis; the `active_refresh_tokens` gauge is removed.
- ~~5~~ Password reset and password change revoke sessions.
- ~~13~~ `ruff format --check` passes.
- ~~14~~ Not reproducible: the virtualenv runs Python 3.14.4.

Fixed in the 2026-11 cleanup (S1.3, S1.4, S1.5):

- ~~6~~ The email-change token is consumed by `POST /auth/email/verify`; `PUT /users/me/email` replaces the email part of `PATCH /user/me`, and a dedicated template is sent to the new address.
- ~~4~~ `/metrics` is served at the exact path and never counted as a request.
- ~~12~~ The `Dockerfile` healthcheck targets `/health`, which works with `METRICS_ENABLED=false`.
- ~~15~~ Admin CSRF: sqladmin runs custom actions as `GET` requests. The admin session cookie is now `SameSite=Strict`, `HttpOnly`, `Secure` outside `dev`/`unittest`, and expires 8 hours after login. A fail-safe Fetch Metadata guard answers 403 to every state-changing admin request (unsafe methods and actions) that does not carry `Sec-Fetch-Site: same-origin`, a missing header included ([OWASP CSRF cheat sheet, Fetch Metadata headers](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)). Browsers without the header (before the March 2023 baseline) cannot log in to the admin or run actions.

Open:

2. `tests/conftest.py` runs `alembic upgrade head` and `Base.metadata.create_all` on the same database, and `asyncio.run()` shares the application's `AsyncEngine` across event loops. [Phase 0]
7. The password-reset email link is relative (`/auth/reset-password?token=`) and lacks `FRONTEND_URL`. [Phase 1]
8. Refresh cookie is set without `Secure`. [Phase 1]
9. `pytest` `--cov-fail-under=80` and `[tool.coverage.report] fail_under = 85` conflict. [Phase 0]
10. Partly fixed: `BCRYPT_ROUNDS`, `CACHE_KEY_PREFIX`, `ROUTE_CACHE_*`, `DEBUG` and `OTLP_*` are removed. Still unused settings: `REDIS_URL` (set by tests). `MAIL_*` values are read through `os.getenv` rather than `Settings`. [Phase 0]
11. Partly fixed: `alembic/env.py` no longer overrides injected variables, but it still loads `.env.local` in dev. [Phase 4]
16. The admin action "Force password change" sets `password_credentials.must_change`, but neither login, refresh nor access tokens check it, and a password change or reset does not clear it. [Phase 3]
17. A locked account can be told apart by response time while the Argon2 queue is loaded: a refusal on a locked account waits like an average verify, but does not queue behind other password evaluations. The real control is rate limiting ([D4](#open-decisions)). [Phase 3]
18. **Accepted risk:** `PUT /users/me/email` answers 409 `email_taken` when the new address belongs to another account, so a signed-in user who knows their password can test whether an address is registered. The owner accepted this: `POST /auth/register` reveals the same with its 409, and only the account holder (after the password check) can ask.
19. `/health/ready` is routed publicly by the Swarm stack; it should be reachable only from the orchestrator and monitoring. [deploy round]
20. On a Linux host shared by several users, the worker's default `PROMETHEUS_MULTIPROC_DIR` (`/tmp/taskiq-metrics`) is a predictable path in the shared `/tmp`: another local user can create it first, plant symlinks or alter the metric files. Containers, Windows and macOS (per-user temp directories) are not affected. The deploy round sets an explicit directory on a tmpfs. [deploy round]
21. **Accepted for now:** there is no dead-letter queue and no delivery cap. A message whose worker dies on every attempt (for example a crash after the mail was sent but before the acknowledgement) is redelivered every 10 minutes without end.
22. **Accepted for now:** when the scheduler fails to send a due retry (a short Redis outage during the send), taskiq's scheduler does not try that retry again until the scheduler restarts.

---

## Open decisions

Decisions deliberately deferred. Each should become an ADR when taken.

| # | Topic | Options | Current lean |
|---|---|---|---|
| D1 | Error response format | keep the `success/data/error/metadata` envelope, or RFC 9457 `application/problem+json` | **Decided 2026-11-16:** RFC 9457 for errors, plain typed responses for success, API under `/api/v1`; the frontend contract changes with it. See [ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md) (implemented in S1 of the 2026-11 cleanup) |
| D2 | Admin panel | keep `sqladmin` and harden it, or remove it from the boilerplate | keep, disabled in `prod` by default |
| D3 | Authorization model | generic RBAC/permissions tables in the boilerplate, or leave `roles`/`permissions` placeholders | add RBAC as its own phase |
| D4 | Rate limiting placement | in-app (`limits` + Redis) plus Traefik, or gateway only | both; app limits are environment-independent |
| D5 | CI platform | GitHub Actions, GitLab CI, other | undecided |
| D6 | Type checker | mypy strict as gate with `ty` advisory, or pyright | mypy + ty advisory |
| D7 | Repository layout | monorepo README at the root plus this file, one AGENTS.md per package | root overview + per-package files |
| D8 | License and compliance scope | internal, or open source; whether CRA/NIS2 deliverables (SBOM, SECURITY.md, reporting process) are mandatory | treat as mandatory for an industrial product |
| D9 | Idempotency keys | implement in the boilerplate now (frontend blueprint requires them) or later | Phase 2 |
| D10 | Primary key strategy | UUIDv4 (current), UUIDv7, bigint | evaluate UUIDv7 with Postgres 18 |
| D11 | Deployment target | Docker Swarm (current), Kubernetes, both | Swarm now, Kubernetes manifests later |
| D12 | Password hashing library | keep `argon2-cffi` directly, or `pwdlib` (FastAPI tutorial default, bcrypt upgrade path) | keep argon2-cffi |

---

## Standards and references

- OWASP API Security Top 10 (2023), OWASP ASVS 5.0, OWASP Cheat Sheets: REST Security, Password Storage, JWT
- RFC 8725 (JWT Best Current Practices), RFC 9457 (Problem Details), RFC 9562 (UUIDv7), RFC 9745 (Deprecation header), RFC 8594 (Sunset header)
- OpenTelemetry HTTP semantic conventions; W3C Trace Context
- The Twelve-Factor App and the "Beyond the Twelve-Factor App" additions (API first, telemetry, auth)
- FastAPI docs: Bigger Applications, Dependencies with yield, OpenTelemetry, Deployment with Docker
- Docker build best practices; uv Docker integration guide; SLSA v1.2; CycloneDX 1.7
- EU Cyber Resilience Act (Regulation (EU) 2024/2847); NIS2 (NIS2UmsuCG in Germany); ISO/IEC 27001:2022
