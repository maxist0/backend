# API Backend Boilerplate

A production-grade, framework-complete REST API skeleton built on **FastAPI**, **PostgreSQL**, **Redis** and **Python 3.14**. It ships authentication, sessions, background jobs, caching, structured logging, metrics and a Docker Swarm deployment, and deliberately **no business features**. Add a domain module, not plumbing.

> **Status:** hardening in progress. The foundation is sound (async everywhere, Argon2id, PyJWT, refresh-token rotation, Testcontainers), but several production gaps are documented in [Roadmap](#roadmap) and [Known issues](#known-issues). Treat `main` as a boilerplate under review, not as a release.

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
| Auth | Register, login (OAuth2 password form), logout, refresh with rotation and reuse detection, email verification, password reset, account lockout, audit log |
| Sessions | Opaque refresh tokens (SHA-256 hashed in Postgres, mirrored in Redis), JWT access tokens (15 min) |
| Users | `GET/PATCH /user/me`, password change, email-change flow (partial, see Known issues) |
| Data | PostgreSQL, Alembic migrations, UUID primary keys, timezone-aware timestamps |
| Caching | Redis via `cashews` route cache with ETag and Cache-Control middleware |
| Background jobs | `taskiq` with Redis broker, result backend, cron scheduler, transactional email tasks |
| Observability | `structlog` JSON logs with request IDs, Prometheus metrics, optional OTLP log export, Grafana/Loki/Tempo/Alloy stack |
| Admin | `sqladmin` panel at `/admin` (users, credentials, sessions, tokens, audit log) |
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
uv run python app.py dev
```

The dev launcher also starts a `taskiq` worker as a child process so emails and scheduled jobs run locally.

| URL | Purpose |
|---|---|
| http://localhost:8000/docs | Swagger UI (disabled in `prod`) |
| http://localhost:8000/redoc | ReDoc |
| http://localhost:8000/metrics/ | Prometheus metrics |
| http://localhost:8000/metrics/health | Health check |
| http://localhost:8000/admin | Admin panel (`admin` / `admin` by default, change it) |

### 5. Verify

```bash
# Lint, format check, types
uvx ruff@0.15.0 check src tests
uvx ruff@0.15.0 format --check src tests
uv run mypy src

# Tests (needs a running Docker daemon; see Known issues for the Redis caveat)
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
│   │   ├── metrics/           # /metrics and /metrics/health
│   │   └── exampleRoute/      # empty template module (to be replaced, see Roadmap)
│   ├── common/                # Cross-cutting infrastructure
│   │   ├── config.py          # pydantic-settings Settings, environments
│   │   ├── database.py        # async engine, session factory, get_db
│   │   ├── dependencies.py    # DbSession = Annotated[AsyncSession, Depends(get_db)]
│   │   ├── cache.py           # Redis pool + cashews setup
│   │   ├── taskiq.py          # broker, scheduler, task registration
│   │   ├── mail.py            # fastapi-mail configuration
│   │   ├── logging.py         # structlog + stdlib bridge + OTLP handler
│   │   ├── middleware.py      # access log, metrics, security headers, request id
│   │   ├── metrics.py         # Prometheus registry and collectors
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
├── documentation/             # Topic docs (partly stale, see Roadmap Phase 7)
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
           │ metrics /metrics, logs stdout (JSON) or OTLP
           ▼
     Prometheus ── Grafana ── Loki ── Tempo ── Alloy (OTLP receiver)
```

### Request lifecycle

Middleware is applied in `src/main.py`; the last one added runs first. Effective order for a request:

1. `AccessLogMiddleware` captures client IP, method, path, duration and emits one `http.access` event.
2. `MetricsMiddleware` records `http_requests_total`, duration and size histograms, active connections.
3. `SecurityHeadersMiddleware` sets `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`.
4. `GZipMiddleware` compresses responses above 1 KB.
5. `CacheRequestControlMiddleware` and `CacheEtagMiddleware` (cashews) honour `Cache-Control` and `If-None-Match` when `ROUTE_CACHE_ENABLED`.
6. `CORSMiddleware` (FastAPI built-in).
7. `MetaDataMiddleware` binds a `request_id` into `structlog` context and returns it as `X-Request-ID` with `X-Process-Time`.
8. Router → dependencies (`DbSession`, `get_current_user`) → handler → service → crud.
9. Exception handlers translate `RequestValidationError`, `AuthenticationError` and unhandled `Exception` into the error envelope.

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

`src/common/config.py` defines one `Settings` class (pydantic-settings). `ENVIRONMENT` selects behaviour:

| Value | Docs | Log format | `.env.local` | Notes |
|---|---|---|---|---|
| `dev` | on | coloured console | read | taskiq worker auto-started by `app.py` |
| `unittest` | on | console | ignored | used by `tests/conftest.py` |
| `test`, `stage` | on | console | ignored | runtime injects variables |
| `prod` | off | JSON | ignored | admin panel still mounted (see Roadmap) |

### Authentication design

```
register ──▶ user + argon2id credential + VERIFY_EMAIL token ──▶ email task
login    ──▶ verify password, lockout check ──▶ JWT access (15 min)
                                             └▶ opaque refresh (32 B urlsafe)
                                                  ├─ sha256 → sessions table (family_id, device, ip)
                                                  ├─ raw    → Redis rt:<token> with TTL (fast path)
                                                  └─ cookie refresh_token, HttpOnly, SameSite=Lax, Path=/auth
refresh  ──▶ Redis hit: revoke old session, issue new pair in same family
         └▶ Redis miss + revoked session in DB: reuse detected → revoke whole family
logout   ──▶ delete Redis key, mark session revoked
```

Access tokens are stateless and remain valid until expiry; revocation only affects refresh tokens. Every auth event is written to `auth_audit_log`.

### Data model

| Table | Purpose | Notable columns |
|---|---|---|
| `users` | identity | `username`, `email` (unique), `is_active`, `is_email_verified`, `is_locked`, `failed_login_attempts`, `locked_until` |
| `password_credentials` | one row per user | `password_hash` (argon2id), `must_change` |
| `sessions` | refresh tokens | `token_hash` (unique), `family_id`, `is_revoked`, `revoke_reason`, `expires_at`, device and IP metadata |
| `email_tokens` | verification, change, reset | `purpose`, `token_hash`, `new_email`, `expires_at`, `used_at`, `invalidated_at` |
| `auth_audit_log` | append-only security log | `event_type`, `ip_address`, `user_agent`, `detail` |

All primary keys are UUIDv4 generated in the application. Timestamps are `timestamptz` in UTC.

### Caching and background work

- **Route cache:** `@cache(ttl=cache_control_ttl(default=settings.ROUTE_CACHE_TTL), key=...)` from `cashews` on idempotent GET routes. ETag and `Cache-Control` request directives are honoured by the two cashews middlewares.
- **Refresh-token cache:** raw token → JSON payload in Redis db 0 with TTL equal to the session lifetime.
- **Jobs:** `taskiq` `ListQueueBroker` on Redis db 1, results in db 2. Tasks are declared with `@broker.task(...)` in `src/utils/`; the scheduler reads `schedule=[{"cron": ...}]` labels. Workers and scheduler are separate Swarm services.

### Observability

- **Logs:** `structlog` with contextvars. Every request has `request_id`; authenticated requests add `user_id` and `username`. Dev renders coloured console output, `prod` renders JSON lines to stdout. Optional OTLP/HTTP export of the same records to Grafana Alloy (`OTLP_LOGS_ENABLED`).
- **Metrics:** custom Prometheus registry exposed at `GET /metrics/`: `http_requests_total`, `http_request_duration_seconds`, request and response size histograms, `active_connections`, `database_operations_total`, `database_operation_duration_seconds`, `api_errors_total`, `application_info`.
- **Traces:** not yet wired (Tempo is deployed, the application does not emit spans; see Roadmap Phase 3).

### Error handling

Responses currently use a project-specific envelope:

```json
{ "success": true,  "data": { ... }, "metadata": { "timestamp": "...", "request_id": "...", "version": "0.1.0", "execution_time": 0.012 } }
{ "success": false, "data": null,    "error": { "code": "VALIDATION_ERROR", "message": "...", "field": "email", "details": { ... } }, "metadata": { ... } }
```

Whether to keep this envelope or move to RFC 9457 Problem Details is an [open decision](#open-decisions).

---

## Design decisions

| Decision | Choice | Why | Alternatives considered |
|---|---|---|---|
| Concurrency | async end to end, no sync DB or Redis calls | FastAPI and uvicorn are ASGI; one blocking call stalls the event loop | sync SQLAlchemy with threadpool |
| Package manager | `uv` with committed `uv.lock` | reproducible installs, fast CI and Docker builds, PEP 735 groups | pip-tools, Poetry |
| Python | 3.14 target | bugfix phase, full wheel coverage, deferred annotations, required by Starlette 1.x and SQLAlchemy 2.1 floors (3.11+) | 3.13 (security-only from 2026-10), 3.15 (too new for all wheels) |
| Password hashing | `argon2-cffi` Argon2id, library defaults (t=3, m=64 MiB, p=4) | OWASP first choice; defaults exceed the OWASP minimum (m=19 MiB, t=2) | bcrypt (72-byte limit), passlib (unmaintained) |
| JWT | `PyJWT[crypto]`, HS256 | maintained, what the FastAPI tutorial uses | python-jose (critical unpatched CVE in 2026) |
| Refresh tokens | opaque, hashed at rest, rotated on every use, family-based reuse detection, HttpOnly cookie | OAuth 2.1 / RFC 9700 guidance for public clients; stolen tokens are detectable | JWT refresh tokens (not revocable without a denylist) |
| Session store | Postgres as source of truth, Redis as fast path | audit trail survives Redis restarts; hot path stays O(1) | Redis only |
| ORM | SQLAlchemy 2 async, `expire_on_commit=False`, explicit `select()` | typed, mature, Alembic autogenerate | SQLModel, raw asyncpg |
| Migrations | Alembic with async `env.py`, run as a separate step before rollout | never migrate at import time; CI can run `alembic check` | app-startup migrations |
| Primary keys | UUID generated client-side | safe to expose, mergeable across environments | bigint identity (planned evaluation of UUIDv7, Postgres 18) |
| Background jobs | `taskiq` + Redis | async-native, FastAPI dependency integration, cron via labels | Celery (no async tasks), arq (maintenance only) |
| Route cache | `cashews` | maintained, ETag and Cache-Control middlewares included | fastapi-cache2 (dormant since 2024) |
| Logging | `structlog` over stdlib, JSON in prod | third-party loggers are captured by the stdlib bridge; machine-readable | loguru, stdlib only |
| Metrics | `prometheus_client` with own registry | pull model, Grafana-native | push to OTLP only |
| Email | `fastapi-mail` + Jinja2 templates, sent from tasks | never block a request on SMTP | synchronous send in handler |
| Admin | `sqladmin` | zero-code CRUD for support staff | custom admin, none |
| Testing | integration tests against real Postgres and Redis (Testcontainers) | catches SQL, migration and cookie behaviour that mocks hide | mocked repositories |
| Deployment | Docker Swarm with Traefik | matches current infrastructure; Kubernetes kept open | Kubernetes, bare uvicorn |
| Scope | no business features | the repo is a boilerplate; domain modules are added per project | sample "items" CRUD |

---

## Security posture

Implemented:

- Argon2id password hashing, constant-time verification through the library
- Account lockout after 5 failed logins for 15 minutes, audit-logged
- Refresh-token rotation with reuse detection and family revocation
- Tokens (refresh, email) stored only as SHA-256 hashes
- Email-enumeration-safe `password/forgot`
- Security headers, GZip, request-id correlation
- OpenAPI docs disabled in `prod`
- Non-root container user, multi-stage image

Not yet implemented (tracked in the [Roadmap](#roadmap)):

- Startup validation that no default secret is in use
- `Secure` flag and `__Host-` prefix on the refresh cookie
- Rate limiting (per IP and per account), request body size limit, request timeout
- JWT `iss`/`aud`/`typ` claims and token-type check; algorithm pinned in code
- Session revocation on password reset and password change
- HSTS, `Cache-Control: no-store`, CSP `frame-ancestors 'none'`; removal of the obsolete `X-XSS-Protection`
- Trusted-proxy handling for `X-Forwarded-For`
- Authentication on `/metrics`, admin panel disabled in `prod` by default
- PII minimisation in logs (usernames, emails, query strings)
- Dependency and image scanning, SBOM, signed images

---

## Configuration

All settings are environment variables read by `Settings` (`src/common/config.py`). The most important ones:

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `dev` | `dev`, `unittest`, `test`, `stage`, `prod` |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/api` | asyncpg driver required at runtime |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `20` | per process |
| `CACHE_HOST` / `CACHE_PORT` / `CACHE_DB` / `CACHE_PASSWORD` | `localhost` / `6379` / `0` / empty | Redis for cache and refresh tokens |
| `ROUTE_CACHE_ENABLED` / `ROUTE_CACHE_TTL` | `true` / `2m` | cashews |
| `TASKIQ_BROKER_URL` / `TASKIQ_RESULT_BACKEND_URL` | Redis db 1 / db 2 | |
| `JWT_SECRET_KEY` | placeholder | **must** be replaced; 256-bit random value |
| `JWT_ALGORITHM` | `HS256` | will be pinned in code (Roadmap Phase 1) |
| `ACCESS_TOKEN_EXPIRE_MINUTES` / `REFRESH_TOKEN_EXPIRE_DAYS` | `15` / `30` | |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` / `ADMIN_SECRET_KEY` | `admin` / `admin` / placeholder | **must** be replaced |
| `MAIL_*` | unset | mail is skipped when incomplete |
| `LOG_LEVEL` / `ACCESS_LOG_ENABLED` / `METRICS_ENABLED` | `INFO` / `true` / `true` | |
| `OTLP_LOGS_ENABLED` / `OTLP_ENDPOINT` | `false` / `http://localhost:4318` | Grafana Alloy |
| `FRONTEND_URL` | `http://localhost:3000` | used in email links |
| `HOST` / `PORT` | `0.0.0.0` / `8000` | |

Secrets must never be committed. `.env` in the repository is a template with empty values; `.env.local` is git-ignored.

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

Set `DB_PASSWORD`, `REDIS_PASSWORD`, `JWT_SECRET_KEY` and the admin credentials in the environment before deploying; the compose fallbacks are development values.

### Monitoring stack

`deployment/docker/monitoring/docker-compose.yml` runs Prometheus, Grafana (dashboards for Postgres, Redis, Docker, Loki and taskiq are provisioned), Loki, Tempo, Alloy, cAdvisor and the Postgres and Redis exporters. See `deployment/docker/monitoring/README.md`.

---

## Roadmap

This is the improved implementation plan. Phases are ordered by risk reduction per effort. Each item is a checkbox so progress is visible in the repo; acceptance criteria are in italics.

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

- [ ] Startup guard: refuse to boot in `stage`/`prod` when `JWT_SECRET_KEY`, `ADMIN_PASSWORD`, `ADMIN_SECRET_KEY` or `DATABASE_URL` still carry defaults; require `len(JWT_SECRET_KEY) >= 32`.
- [ ] Pin the JWT algorithm allowlist in code; add `iss`, `aud`, `typ=access`; verify `type` on decode; reject tokens without `jti`. *RFC 8725 checklist satisfied.*
- [ ] Refresh cookie: `Secure=True` outside `dev`, `SameSite=Strict` where the frontend allows it, `__Host-` prefix, explicit `max_age`.
- [ ] Revoke all sessions on password reset and password change; add `POST /auth/logout-all`.
- [ ] Complete the email-change flow: `POST /auth/email/confirm` consuming `CHANGE_EMAIL` tokens, dedicated template, `EMAIL_CHANGED` audit event, `FRONTEND_URL` in the reset link.
- [ ] Rate limiting with `limits` + Redis: per IP on `/auth/*`, per account on login; return `429` with `Retry-After`. Decouple lockout from DoS by lockout-by-IP-window instead of hard account locks.
- [ ] Uniform credential error (`Invalid username or password`) for locked, inactive and unknown accounts; log the real reason server-side only.
- [ ] Security headers: add HSTS (behind TLS), `Cache-Control: no-store` on authenticated responses, `Content-Security-Policy: frame-ancestors 'none'`; drop `X-XSS-Protection`.
- [ ] Trusted proxies: run uvicorn with `--proxy-headers --forwarded-allow-ips=<traefik>`; remove manual `X-Forwarded-For` parsing; use the same client IP source for access logs and audit logs.
- [ ] CORS origins, methods and headers from `Settings` (`CORS_ORIGINS` list); delete the dead custom `CORSMiddleware` class.
- [ ] Admin panel: Argon2 hash for the admin password with constant-time compare, `ADMIN_ENABLED` flag defaulting to `false` in `prod`, hide `token_hash` columns.
- [ ] Protect `/metrics` (network policy or bearer token); remove the `KEYS` scan.
- [ ] Request limits: `max_body_size`, per-request timeout, `strict_content_type` kept on.
- [ ] Pydantic request models with `extra="forbid"`; separate create/update/response schemas (OWASP API3).
- [ ] `SECURITY.md` with disclosure process and supported versions.

### Phase 2: API contract

- [ ] API versioning: mount all routers under `/api/v1`; keep `/metrics` and health unversioned; align Traefik `stripprefix`. *Old paths return 404 after one deprecation window.*
- [ ] Decide the error format (see Open decisions) and implement one consistent handler: validation, auth, named domain exceptions, unhandled 500 (no internals leaked).
- [ ] Named exceptions in services instead of `(None, "message")` tuples; routers stop catching `Exception`. Feature `exceptions.py` inherit from `src/common/exceptions.py` base classes mapped to status codes.
- [ ] `response_model` on every route; drop `top_level_fields`; let FastAPI serialise Pydantic directly (0.130+). Swagger login works through the standard `TokenResponse`.
- [ ] Pagination helper: cursor-based for lists, bounded `limit` (default 20, max 100), stable ordering.
- [ ] Optional `Idempotency-Key` support for POST (Redis-backed, 24 h TTL, 409 on payload mismatch) to match the frontend blueprint.
- [ ] `ETag`/`If-Match` on mutable resources (412 on mismatch).
- [ ] Health endpoints: `GET /health/live` (no dependencies), `GET /health/ready` (DB `SELECT 1`, Redis `PING`, 503 on failure or during shutdown), `GET /health/startup`. Update the Dockerfile `HEALTHCHECK` and Swarm probes.
- [ ] OpenAPI: `summary`, `description`, `responses` per route; export the schema in CI and lint it (Spectral or vacuum); diff against the previous release (oasdiff).
- [ ] Timezone-aware timestamps everywhere (`datetime.now(timezone.utc)`), RFC 3339 output.

### Phase 3: Reliability and observability

- [ ] Replace `BaseHTTPMiddleware` implementations with pure ASGI middleware (streaming-safe, lower overhead).
- [ ] Tracing: enable FastAPI native OpenTelemetry (`FastAPI(telemetry=...)`, 0.142+) plus SQLAlchemy, asyncpg, redis and httpx instrumentation; export OTLP to Alloy → Tempo; propagate W3C `traceparent`; add `trace_id`/`span_id` to log records.
- [ ] Logs: redact PII (no emails, usernames or query strings in `http.access`; hash or drop IPs per policy); use `opentelemetry-instrumentation-logging` instead of the deprecated `LoggingHandler` path; keep `uvicorn.error` at INFO.
- [ ] Metrics: align with OTel HTTP semantic conventions (`http.server.request.duration` in seconds with the standard buckets, `http.route` label); add cache hit/miss, task queue depth, DB pool usage; drop the broken `active_refresh_tokens` gauge or compute it from Postgres.
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

Verified during the November 2026 review. None are fixed yet; the Roadmap phase is given in brackets.

1. The test suite starts a Redis Testcontainer but the application reads `CACHE_HOST`/`CACHE_PORT`, not `REDIS_URL`, so tests require a local Redis on `localhost:6379` and ignore the container. [Phase 0]
2. `tests/conftest.py` runs `alembic upgrade head` and `Base.metadata.create_all` on the same database, and `asyncio.run()` shares the application's `AsyncEngine` across event loops. [Phase 0]
3. `/metrics/` scans Redis with `KEYS rt:` (no wildcard), so `active_refresh_tokens` is always 0 and the scan is O(N). [Phase 3]
4. `MetricsMiddleware` skips `/metrics` but the router is mounted at `/metrics/`, so scrapes are counted as requests. [Phase 3]
5. Password reset and password change do not revoke existing sessions; `documentation/authentication.md` claims they do. [Phase 1]
6. `PATCH /user/me` with a new email creates a `CHANGE_EMAIL` token but no endpoint consumes it, and the verification email template is reused. [Phase 1]
7. The password-reset email link is relative (`/auth/reset-password?token=`) and lacks `FRONTEND_URL`. [Phase 1]
8. Refresh cookie is set without `Secure`. [Phase 1]
9. `pytest` `--cov-fail-under=80` and `[tool.coverage.report] fail_under = 85` conflict. [Phase 0]
10. `BCRYPT_ROUNDS` and `CACHE_KEY_PREFIX` are configured but unused; `REDIS_URL` is set by tests but unused. [Phase 0]
11. `alembic/env.py` loads `.env.local` with `override=True`, which can override injected variables inside containers if the file is present. [Phase 4]
12. `Dockerfile` healthcheck targets `/metrics/health` and fails when `METRICS_ENABLED=false`. [Phase 2]
13. `ruff format --check` fails on `src/admin.py`. [Phase 0]
14. The project virtualenv on the development machine is broken (`uv trampoline failed to spawn Python`); run `uv sync --reinstall`. [Phase 0]

---

## Open decisions

Decisions deliberately deferred. Each should become an ADR when taken.

| # | Topic | Options | Current lean |
|---|---|---|---|
| D1 | Error response format | keep the `success/data/error/metadata` envelope, or RFC 9457 `application/problem+json` | RFC 9457 for errors, plain `response_model` for success; frontend contract must change with it |
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
