# API Backend Boilerplate

A module boilerplate for REST APIs on **FastAPI**, **PostgreSQL**, **Redis** and **Python 3.14**. A stable **core** ships user accounts, authentication and sessions, background jobs, caching, structured logging, metrics and a Docker Swarm deployment. It contains **no business features**: you build an app by adding modules and you do not edit the core.

> **Status (2026-11-22):** the 2026-11 cleanup is finished (core and modules, RFC 9457 errors under `/api/v1`, reliable background jobs, strict ruff, mypy and pyright, documentation). Still open: the frontend migration to the new API contract, the update of the Swarm files under `deployment/` (the deploy round), and the security and reliability items in the [Roadmap](#roadmap) and [Known issues](#known-issues). Treat `main` as a boilerplate under review, not as a release. The review and the cleanup plan are in [documentation/review-2026-11/](documentation/review-2026-11/README.md).

Conventions for contributors and coding agents are in [AGENTS.md](AGENTS.md). Read it before you change code.

## Contents

1. [What you get](#what-you-get)
2. [Quick start](#quick-start)
3. [Project layout](#project-layout)
4. [How it fits together](#how-it-fits-together)
5. [Design decisions](#design-decisions)
6. [Configuration](#configuration)
7. [Commands](#commands)
8. [Testing](#testing)
9. [Deployment](#deployment)
10. [Documentation](#documentation)
11. [Roadmap](#roadmap)
12. [Known issues](#known-issues)
13. [Open decisions](#open-decisions)
14. [Standards and references](#standards-and-references)

## What you get

Versions are the locked ones in `uv.lock`.

| Area | Implemented today |
|---|---|
| Framework | FastAPI 0.163 on Starlette 1.10, Pydantic 2.14, fully async: SQLAlchemy 2.1 with psycopg 3, redis-py 8 |
| Modules | A `Module` dataclass and one tuple, `MODULES`; an app adds modules and never edits the core ([ADR 0001](documentation/adr/0001-core-and-module-contract.md), [ADR 0008](documentation/adr/0008-module-layout-and-users-auth-split.md), guide: [modules.md](documentation/modules.md)) |
| Users | Built-in `users` module: `GET` and `PATCH /api/v1/users/me`, the `CurrentUser` and `OptionalUser` dependencies |
| Auth | Built-in `auth` module: register, login (OAuth2 password form), refresh with rotation and reuse detection, logout and logout-all, email verification, password reset, password and email change, account lockout, audit log ([authentication.md](documentation/authentication.md)) |
| API contract | Everything under `/api/v1`, plain typed responses, RFC 9457 `application/problem+json` errors with a stable `code` ([ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md), frontend view: [backend-contract.md](../frontend/documentation/backend-contract.md)) |
| Data | PostgreSQL 18, Alembic migrations (async `env.py`, post-write ruff hooks), model mixins for UUIDv7 primary keys and `timestamptz` columns, deterministic constraint names, enums as checked VARCHAR values ([ADR 0012](documentation/adr/0012-schema-conventions-uuidv7-value-enums-usernames.md)) |
| Caching | `@cached(ttl, key)` and `invalidate(pattern)` on Redis (cashews, JSON values), `RedisDep` for direct access ([redis.md](documentation/redis.md)) |
| Background jobs | taskiq 0.13 on a Redis stream with acknowledgements, delayed retries, a cron scheduler, a database session per task, transactional mail tasks |
| Observability | structlog JSON logs on stdout with request IDs, Prometheus metrics at `/metrics`, `/health` and `/health/ready` ([logging.md](documentation/logging.md), [monitoring.md](documentation/monitoring.md)) |
| Admin | sqladmin panel at `/admin`: read-only users, sessions and audit log; every change is an audited action through the auth services; on by default only in `dev` |
| Tooling | uv, ruff (strict rule set), mypy strict and pyright strict as type gates, pytest with Testcontainers, multi-stage Dockerfile |
| Deployment | Docker Swarm stack with Traefik and a monitoring stack in `deployment/` (git-ignored in this repository) |

## Quick start

### Prerequisites

- [uv](https://docs.astral.sh/uv/) (verified with 0.13.8). It installs Python 3.14 for you.
- Docker Desktop or Docker Engine, for Postgres, Redis and the integration tests.
- Git.

### 1. Install

```bash
git clone <repo-url> backend
cd backend
uv python install 3.14
uv sync
```

`uv sync` creates `.venv` and installs the locked dependencies, the `dev` group (test and lint tools) included.

### 2. Start Postgres and Redis

```bash
docker compose -f compose.dev.yaml up -d --wait
```

`compose.dev.yaml` starts Postgres 18 and Redis 7 on `127.0.0.1:5432` and `127.0.0.1:6379`, with the public development credentials that match the defaults in `Settings`. Never use it outside a developer machine. If a port is taken, set `DEV_POSTGRES_PORT` or `DEV_REDIS_PORT` for the command and change `DATABASE_URL`, `CACHE_PORT` and the `TASKIQ_*_URL` values in `.env.local`. Stop with `docker compose -f compose.dev.yaml down`; add `-v` to delete the data.

### 3. Configure

Copy the template:

```bash
cp .env .env.local          # PowerShell: Copy-Item .env .env.local
```

`.env` is the tracked reference of every variable. Each active line holds the default from `Settings`, which matches `compose.dev.yaml`. Commented out are `ENVIRONMENT` (process environment only), `APP_VERSION` (the installed package version), the secrets and the settings that are unset by default (Redis password, mail account). An unedited copy therefore changes nothing for `run.py dev`, and it gives Alembic the `DATABASE_URL` it needs: Alembic builds no `Settings` and reads only that variable. `tests/unit/core/test_env_template.py` proves both.

`.env.local` is git-ignored and read only when `ENVIRONMENT=dev` (`python run.py dev`, or `ENVIRONMENT=dev` for Alembic). Change the values you need there. A real environment variable always wins over `.env.local`. Every other environment expects variables injected by the runtime (Docker, Swarm, CI).

### 4. Migrate and run

```bash
ENVIRONMENT=dev uv run alembic upgrade head      # PowerShell: $env:ENVIRONMENT = "dev"; uv run alembic upgrade head
uv run python run.py dev
```

Without `ENVIRONMENT=dev`, Alembic ignores `.env.local` and stops with "DATABASE_URL is not set" unless you export `DATABASE_URL`.

**Database created before 2026-11-22?** The migrations were replaced by one baseline revision on that day, so an older local database is stamped with a revision that no longer exists, and Alembic stops with "Can't locate revision". Delete it with `docker compose -f compose.dev.yaml down -v`, start it again and migrate ([database.md → Revisions](documentation/database.md#revisions)). Do not `alembic stamp` it onto the baseline: its schema is not the baseline's.

`run.py dev` sets `ENVIRONMENT=dev` and starts three processes:

| Process | What it does |
|---|---|
| API | uvicorn on `http://localhost:8000` |
| taskiq worker | runs background tasks (mail, cleanup) |
| taskiq scheduler | sends cron tasks and delayed retries; without it a failed task is never retried ([ADR 0009](documentation/adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md)) |

`run.py` stops the worker and the scheduler when you stop it.

**Windows.** psycopg cannot run on Windows' default `ProactorEventLoop`. `run.py` starts uvicorn on a `SelectorEventLoop`, and it starts the worker as `python -m app.worker`, one process on a `SelectorEventLoop`, instead of the `taskiq worker` CLI. `TASKIQ_WORKERS` therefore has no effect there. According to the comment in `src/app/core/database.py`, `fastapi dev` works too and `fastapi run` needs `--workers 2` or more (untested here).

| URL | Purpose |
|---|---|
| http://localhost:8000/docs | Swagger UI (off in `prod`) |
| http://localhost:8000/redoc | ReDoc (off in `prod`) |
| http://localhost:8000/health | Liveness |
| http://localhost:8000/health/ready | Readiness (PostgreSQL and Redis) |
| http://localhost:8000/metrics | Prometheus metrics |
| http://localhost:8000/admin | Admin panel (mounted by default only in `dev`; `admin` / `admin` by default) |

To add your own feature, follow [documentation/modules.md](documentation/modules.md).

## Project layout

```
backend/
├── run.py                     # launcher: python run.py [dev|unittest] [--host] [--port] [--reload]
├── pyproject.toml             # dependencies, uv groups, ruff, mypy, pyright, pytest, coverage
├── uv.lock                    # locked dependency graph (commit it)
├── compose.dev.yaml           # local Postgres 18 and Redis 7
├── Dockerfile                 # multi-stage build, non-root runtime
├── .env                       # tracked template; copy to .env.local (git-ignored)
├── alembic.ini, alembic/      # migrations (async env.py, ruff post-write hooks)
├── src/app/
│   ├── main.py                # create_app(): middleware, routers of MODULES, lifespan
│   ├── worker.py              # taskiq worker and scheduler root (python -m app.worker)
│   ├── core/                  # infrastructure; public facade in __init__.py (__all__)
│   │   ├── config.py          # Settings, get_settings, SettingsDep, startup guard
│   │   ├── database.py        # engine, session factory, DbSession, Base, utcnow
│   │   ├── errors.py          # AppError family, RFC 9457 handlers, DB error details
│   │   ├── security.py        # JWT, Argon2 password hashing, token hashing
│   │   ├── middleware.py      # request id, access log, HTTP metrics, security headers
│   │   ├── health.py          # /health, /health/ready, /metrics
│   │   ├── cache.py           # RedisDep, cached, invalidate
│   │   ├── tasks.py           # task decorator, TaskDbSession, broker, retry, scheduler
│   │   ├── mail.py            # fastapi-mail wrapper, send_templated_email
│   │   ├── logging.py         # structlog setup, get_logger
│   │   ├── module.py          # the Module contract
│   │   ├── admin.py           # sqladmin factory, CSRF guard, action helpers
│   │   └── templates/         # base.html, the email layout
│   └── modules/
│       ├── __init__.py        # MODULES: the registered modules
│       ├── users/             # User model, profile routes, CurrentUser, OptionalUser
│       └── auth/              # credentials, sessions, tokens, audit log, flows, admin views
├── tests/
│   ├── helpers.py             # shared test helpers
│   ├── unit/                  # no Docker: core/, modules/<name>/
│   └── integration/           # Testcontainers: core/, contract/, modules/<name>/, admin/
├── deployment/docker/         # Swarm stack, monitoring, network (git-ignored, see Deployment)
└── documentation/             # topic pages, ADRs, review-2026-11/
```

A module uses only these file names, and only those it needs: `__init__.py`, `router.py`, `schemas.py`, `service.py`, `crud.py`, `models.py`, `dependencies.py`, `errors.py`, `tasks.py`, `admin.py` and a `templates/` folder. A unit test enforces this. The rules are in [AGENTS.md → Module layout](AGENTS.md#module-layout).

## How it fits together

- **Core and modules.** Imports point one way: `core ← users ← auth ← your modules`. A module imports infrastructure from `app.core` only. `create_app()` mounts every module router under `API_PREFIX`; the worker imports each module's tasks and Alembic each module's models. Details: [general-structure.md](documentation/general-structure.md).
- **Requests.** One pure-ASGI middleware ([ADR 0005](documentation/adr/0005-single-asgi-request-middleware.md)) sets the request id and the security headers, writes the access log and records the HTTP metrics. A route calls one service function; the service owns the transaction.
- **Errors.** Services raise `AppError` subclasses. Handlers render them as RFC 9457 problem responses with a `code` and the `request_id` ([api.md](documentation/api.md)).
- **Background work.** The API enqueues taskiq tasks after the commit; a worker runs them. Task arguments hold row ids only ([redis.md](documentation/redis.md)).
- **Identity.** Passwords use Argon2id, access tokens are short-lived JWTs, refresh tokens are opaque and live only in Postgres ([authentication.md](documentation/authentication.md)).

## Design decisions

Larger decisions have an ADR in [documentation/adr/](documentation/adr/README.md).

| Decision | Choice | Why | Alternatives considered |
|---|---|---|---|
| Concurrency | async end to end, no sync DB or Redis calls | FastAPI and uvicorn are ASGI; one blocking call stalls the event loop | sync SQLAlchemy with a thread pool |
| Extension model | `Module` dataclass and an explicit `MODULES` tuple ([ADR 0001](documentation/adr/0001-core-and-module-contract.md), [0008](documentation/adr/0008-module-layout-and-users-auth-split.md)) | an app adds modules and never edits the core; one place lists them | auto-discovery, router-only list |
| API contract | `/api/v1`, plain responses, RFC 9457 errors ([ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md)) | a standard error format; no envelope to unwrap | success/error envelope |
| Package manager | `uv` with a committed `uv.lock` | reproducible installs, fast CI and Docker builds, PEP 735 groups | pip-tools, Poetry |
| Python | 3.14 | bugfix phase, full wheel coverage, deferred annotations | 3.13 (security-only from 2026-10), 3.15 (too new for all wheels) |
| Database driver | psycopg 3 (async) | one maintained driver for the app, Alembic and the test harness | asyncpg |
| Password hashing | `argon2-cffi` Argon2id; parameters from `Settings`, run in a thread pool behind a concurrency limiter, rehash on login | OWASP first choice; defaults exceed the OWASP minimum (m=19 MiB, t=2) | bcrypt (72-byte limit), passlib (unmaintained) |
| JWT | `PyJWT[crypto]`, HS256 pinned in code | maintained; a fixed algorithm list prevents algorithm confusion | python-jose (unpatched critical CVE) |
| Refresh tokens | opaque, hashed at rest, rotated on every use, family-based reuse detection, HttpOnly cookie, Postgres only | OAuth 2.1 and RFC 9700 guidance; one atomic `UPDATE ... RETURNING` is the only gate | JWT refresh tokens, a Redis mirror |
| ORM and migrations | SQLAlchemy 2 async with `expire_on_commit=False`; Alembic with an async `env.py`, run as a separate step before rollout | typed, mature, autogenerate; never migrate at import time | SQLModel, app-startup migrations |
| Primary keys | UUIDv7 generated in the application (`UUIDPrimaryKey` mixin; [ADR 0012](documentation/adr/0012-schema-conventions-uuidv7-value-enums-usernames.md)) | safe to expose, mergeable across environments, time-ordered inserts into the key index | UUIDv4 (random index writes), PostgreSQL `uuidv7()` (id unknown before the flush), bigint identity (enumerable) |
| Cache | cashews behind `@cached` / `invalidate`, JSON values ([ADR 0003](documentation/adr/0003-caching-via-cached-decorator.md)) | maintained; caches return values, not responses; nothing is unpickled | fastapi-cache2 (dormant), an ETag middleware |
| Background jobs | taskiq on a Redis stream with acknowledgements ([ADR 0009](documentation/adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md), [0010](documentation/adr/0010-token-emails-rekeyed-in-the-worker.md)) | async-native, FastAPI-style dependencies, cron via labels, at-least-once delivery | Celery (no async tasks), arq (maintenance only) |
| Logging | structlog over stdlib, to stdout only ([ADR 0004](documentation/adr/0004-logging-stdout-only.md)) | third-party loggers share one pipeline; a collector reads the container output | loguru, application-side export |
| Metrics | `prometheus_client` default registry | pull model, Grafana-native, process and GC metrics included | push to OTLP only |
| Email | `fastapi-mail` and Jinja2 templates, sent from tasks | never block a request on SMTP | synchronous send in the handler |
| Admin | `sqladmin`, read-only views with audited actions ([ADR 0006](documentation/adr/0006-slim-admin-via-services.md)) | support staff need to lock and unlock users without raw writes | custom admin, none |
| Testing | unit tests without Docker, integration tests on real Postgres and Redis ([ADR 0007](documentation/adr/0007-test-split-unit-integration.md)) | fast loop for pure logic; catches SQL, migration and cookie behaviour that mocks hide | mocked repositories, a backend e2e suite |
| Deployment | Docker Swarm with Traefik | matches current infrastructure; Kubernetes kept open (D11) | Kubernetes, bare uvicorn |
| Scope | no business features | the repository is a boilerplate; domain code goes into modules | a sample "items" CRUD |

## Configuration

Every setting is an environment variable read by `Settings` (`src/app/core/config.py`). `ENVIRONMENT` is required and has no default. `.env.local` is read only for `dev`. Priority: environment variables, then `.env.local` (`dev` only), then the defaults below. Validation errors never print values.

`ENVIRONMENT` selects behaviour:

| Value | API docs | Log format | `.env.local` | Notes |
|---|---|---|---|---|
| `dev` | on | coloured console | read | `run.py dev` also starts the worker and scheduler; admin panel on by default |
| `unittest` | on | JSON | ignored | test suite; startup guard off |
| `test`, `stage` | on | JSON | ignored | the runtime injects variables; startup guard on |
| `prod` | off | JSON | ignored | startup guard on; admin panel only with `ADMIN_ENABLED=true` |

**Startup guard** (every environment except `dev` and `unittest`). The application refuses to start, and names the offending variables, when `JWT_SECRET_KEY` is the public default or shorter than 32 characters, when `FRONTEND_URL` is the development default or not `https`, or, with the admin panel enabled, when `ADMIN_PASSWORD` or `ADMIN_SECRET_KEY` is a default or shorter than 32 characters.

**App**

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | required | `dev`, `test`, `stage`, `prod`, `unittest` |
| `APP_NAME` | `api` | OpenAPI title, `app_info` metric and the mail templates' `app_name` |
| `APP_DESCRIPTION` | `API Description` | OpenAPI description |
| `APP_VERSION` | installed package version | `[project].version` in `pyproject.toml` is the only source; `0.0.0+unknown` without installed metadata |
| `API_PREFIX` | `/api/v1` | Path prefix of every module router and of the refresh cookie path (`<API_PREFIX>/auth`). Empty, or segments of `[A-Za-z0-9._~-]` without a trailing slash. `/`, `/health`, `/health/ready` and `/metrics` stay unprefixed |
| `FRONTEND_URL` | `http://localhost:5173` | Base of the links in emails: `http(s)://host[:port][/path]`, no userinfo, query or fragment; a trailing slash is ignored. Guarded outside `dev`/`unittest` |
| `FRONTEND_VERIFY_EMAIL_PATH` | `/user/email-bestaetigen` | Frontend page of the verification and email-change links, appended to `FRONTEND_URL` with `?token=`. Absolute path of non-empty segments, no scheme, host, query or fragment |
| `FRONTEND_RESET_PASSWORD_PATH` | `/user/passwort-zuruecksetzen` | The same for the password-reset link |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | JSON list of browser origins allowed with credentials. `*` and values with a path are refused in every environment |
| `HOST` | `0.0.0.0` | Bind address |
| `PORT` | `8000` | Bind port |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Comma-separated proxy addresses whose `X-Forwarded-For` and `-Proto` uvicorn trusts. Set it to the reverse proxy's addresses, or every client IP is the proxy's |

**Database** (psycopg 3). Pool values are per process: every API process and every taskiq worker process has its own pool, so size PostgreSQL's `max_connections` for both.

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost:5432/api` | `postgresql://` and `postgres://` are rewritten to `postgresql+psycopg://`; any other driver, `+asyncpg` included, is refused |
| `DB_CONNECT_TIMEOUT` | `10` | Seconds to open one connection; `>= 2` |
| `DB_POOL_SIZE` | `10` | Connections kept per process |
| `DB_MAX_OVERFLOW` | `20` | Extra connections per process under load |
| `DB_POOL_TIMEOUT` | `30` | Seconds to wait for a free connection |
| `DB_POOL_RECYCLE` | `1800` | Seconds after which a connection is replaced |

**Redis, cache and taskiq.** The broker's Redis must not evict keys (`noeviction`).

| Variable | Default | Notes |
|---|---|---|
| `CACHE_HOST` / `CACHE_PORT` / `CACHE_DB` | `localhost` / `6379` / `0` | Redis for `RedisDep` and `@cached` |
| `CACHE_PASSWORD` | unset | Redis password |
| `CACHE_MAX_CONNECTIONS` | `10` | Connection cap of the Redis client and of the cache |
| `TASKIQ_BROKER_URL` | `redis://localhost:6379/1` | Task stream and retry schedules |
| `TASKIQ_RESULT_BACKEND_URL` | `redis://localhost:6379/2` | Task results (1 h TTL, JSON) |
| `TASKIQ_WORKERS` | `2` | `1..64`. Worker processes started by `run.py dev` (always one on Windows) |
| `TASKIQ_METRICS_HOST` | `127.0.0.1` | IP address or hostname of the worker's Prometheus endpoint. A container sets `0.0.0.0` |
| `TASKIQ_METRICS_PORT` | `9000` | `1..65535` |
| `TASKIQ_RETRY_COUNT` | `3` | `0..10`. Retries after the first run of a task that raises `TransientTaskError` |
| `TASKIQ_RETRY_DELAY_SECONDS` | `60` | `30..3600`. Wait before each retry, plus up to 1 s jitter and up to the scheduler's update interval |

The worker also reads `PROMETHEUS_MULTIPROC_DIR` (not a setting; default `<tmp>/taskiq-metrics`), where its processes write their metrics. The directory must start empty.

**Authentication**

| Variable | Default | Notes |
|---|---|---|
| `JWT_SECRET_KEY` | public placeholder | **Secret.** Guarded outside `dev`/`unittest`: not the default, at least 32 characters. Use a 256-bit random value. The algorithm is HS256, pinned in code |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `30` | |
| `SESSION_MAX_LIFETIME_DAYS` | `90` | `1..365`, at least `REFRESH_TOKEN_EXPIRE_DAYS`. Absolute lifetime of a login: refreshing never extends a session past it ([authentication.md](documentation/authentication.md#tokens)) |
| `ARGON2_TIME_COST` | `3` | `>= 2` (OWASP minimum), validated in every environment |
| `ARGON2_MEMORY_KIB` | `65536` | `>= 19456` (OWASP minimum) |
| `ARGON2_PARALLELISM` | `4` | `>= 1` |
| `PASSWORD_HASH_CONCURRENCY` | `4` | `>= 1`. Concurrent Argon2 operations per process (the bound is N × this value with N processes); each needs `ARGON2_MEMORY_KIB` of RAM |
| `LOGIN_MAX_ATTEMPTS` | `5` | `1..100` (NIST SP 800-63B-4 3.2.2). Consecutive failed password logins that set a timed lock |
| `LOGIN_LOCKOUT_SECONDS` | `900` | `1..86400`. Length of the timed lock |
| `AUTH_CLEANUP_RETENTION_DAYS` | `30` | `1..365`. Days that ended sessions and closed email tokens are kept before the nightly cleanup deletes them; a session counts as ended when its family has ended |

**Mail.** Without server, sender, username and password the mail tasks send nothing and log `email.not_sent` (`reason=mail_not_configured`).

| Variable | Default | Notes |
|---|---|---|
| `MAIL_SERVER` | unset | SMTP host |
| `MAIL_PORT` | `587` | |
| `MAIL_STARTTLS` | `true` | |
| `MAIL_USERNAME` | unset | |
| `MAIL_PASSWORD` | unset | **Secret** |
| `MAIL_FROM` | unset | Sender address |

**Admin panel**

| Variable | Default | Notes |
|---|---|---|
| `ADMIN_ENABLED` | unset | Unset means enabled only in `dev`. Set `true` to mount the panel elsewhere; the Swarm stack sets `false` |
| `ADMIN_USERNAME` | `admin` | |
| `ADMIN_PASSWORD` | `admin` | **Secret.** Guarded when the panel is enabled outside `dev`/`unittest`: not the default, at least 32 characters |
| `ADMIN_SECRET_KEY` | public placeholder | **Secret.** Signs the admin session; the same guard as `ADMIN_PASSWORD` |

**Logging, metrics and health**

| Variable | Default | Notes |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Logs go to stdout only. taskiq's loggers never go below `INFO` |
| `ACCESS_LOG_ENABLED` | `true` | The `http.access` event |
| `METRICS_ENABLED` | `true` | Mounts `GET /metrics` and records the HTTP metrics |
| `READINESS_TIMEOUT_SECONDS` | `2.0` | `> 0`, `<= 30`. Bound for each check of `GET /health/ready` |

Secrets never go into git. `.env` is a template, `.env.local` is git-ignored. A new setting goes into `config.py`, the `.env` template and these tables ([AGENTS.md → Definition of done](AGENTS.md#definition-of-done)). More on environments and secrets: [environment.md](documentation/environment.md).

## Commands

Run all commands from `backend/`. These are the gates; all of them must pass before a change is done.

```bash
uv run ruff check src tests run.py alembic           # lint
uv run ruff format --check src tests run.py alembic  # formatting
uv run mypy                                          # types, strict (src and tests)
uv run pyright                                       # second type gate, strict
uv run pytest                                        # full suite and coverage gate; needs Docker
uv audit --frozen                                    # known vulnerabilities of the locked dependencies
uv lock --check                                      # uv.lock matches pyproject.toml
```

More commands:

```bash
uv run ruff format src tests run.py alembic          # format; pass explicit paths, ruff also formats Markdown code blocks
uv run pytest tests/unit --no-cov                    # fast loop without Docker (a unit-only run cannot reach the coverage gate)
uv run pytest tests/integration -k "refresh" -x      # a subset
uv run ty check                                      # informational only (ADR 0011), not a gate
uv run alembic upgrade head                          # apply migrations to your local database
uv run alembic revision --autogenerate -m "add_x"    # new migration; ruff fixes and formats it, then review it
docker build -t api:latest .                         # image
```

Use `uv run ...`; never `pip install` or activate a virtualenv by hand. If `.venv` is broken, run `uv sync --reinstall`.

## Testing

- **Unit tests** (`tests/unit/`) need no Docker and run in seconds. If the code under test needs no database or Redis, it is a unit test.
- **Integration tests** (`tests/integration/`) use `TestClient` against Postgres and Redis Testcontainers. The schema comes from Alembic. The autouse `clean_db` fixture truncates every table before each test. Tasks run in-process, so a failing task fails the test.
- **Helpers** live in `tests/helpers.py`: `register`, `login`, `bearer`, `register_and_login`, the `Db` SQL helper, `run_task`, `log_events`, the `captured_emails` fixture's `CapturedEmails` and more. Add a shared helper there instead of copying one into a test module.
- **OpenAPI snapshot.** `tests/unit/core/openapi_snapshot.json` is the committed API contract. After an intended contract change, regenerate it and review the diff: `UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/core/test_openapi_contract.py`.
- **Coverage.** `pytest` adds `--cov` through `addopts`; the threshold (`fail_under` in `[tool.coverage.report]`) applies to the full run.

The strategy, fixtures and examples are in [documentation/testing.md](documentation/testing.md); the decision is [ADR 0007](documentation/adr/0007-test-split-unit-integration.md).

## Deployment

**Image.** `docker build -t api:latest .` builds two stages on `python:3.14-slim`: the builder runs `uv sync --frozen --no-dev` and installs the package, the runtime copies the virtualenv and the rest of the build context and runs as `appuser`. The entry point is `python run.py`; `ENVIRONMENT` must be injected. Hardening (digest-pinned base, cache mounts, read-only root file system) is Roadmap Phase 6.

**Swarm stack.** `deployment/docker/api/docker-compose.yml` defines Traefik v2.11, Postgres 16, Redis 7, a one-shot `migrate` service, three API replicas, two taskiq workers, one scheduler and the taskiq dashboard. `deployment/docker/monitoring/` holds Prometheus, Grafana, Loki, Tempo, Alloy and the exporters. `deployment/` is git-ignored in this repository, and it has not been updated for the `/api/v1` contract yet: the deploy round is a separate task (known issues 19 and 20, Roadmap Phase 2 and 6). The commands are:

```bash
docker swarm init                      # once
docker network create -d overlay --attachable monitoring
docker build -t api:latest .
docker stack deploy -c deployment/docker/api/docker-compose.yml --with-registry-auth prod
docker stack rm prod
```

Export `JWT_SECRET_KEY` in the shell that runs `docker stack deploy`; `docker stack deploy` does not read `.env`. The compose file fails the deploy when the value is missing. Set `DB_PASSWORD` and `REDIS_PASSWORD` the same way. The stack sets `ADMIN_ENABLED=false`. These commands are untested in this documentation pass: agents never deploy.

## Documentation

| Page | What it covers |
|---|---|
| [modules.md](documentation/modules.md) | How-to: add a module, with a complete example |
| [general-structure.md](documentation/general-structure.md) | Architecture: composition roots, lifespan, request lifecycle, topology |
| [api.md](documentation/api.md) | Reference: routes, error format, status codes, response conventions |
| [authentication.md](documentation/authentication.md) | Authentication design, endpoints, locks, sessions, security posture |
| [database.md](documentation/database.md) | Engine, sessions, tables, migrations |
| [redis.md](documentation/redis.md) | Cache, taskiq broker, retries, scheduler |
| [logging.md](documentation/logging.md), [monitoring.md](documentation/monitoring.md) | Logs, metrics, health, alerting |
| [development.md](documentation/development.md), [testing.md](documentation/testing.md) | Daily work and tests |
| [environment.md](documentation/environment.md), [docker.md](documentation/docker.md) | Environments, secrets, staging, container commands |
| [adr/](documentation/adr/README.md) | Architecture decision records |
| [review-2026-11/](documentation/review-2026-11/README.md) | The 2026-11 review and the cleanup plan (German) |

## Roadmap

This is the original implementation plan. The re-prioritised plan from the November 2026 review (phases 0 to 6) is in [documentation/review-2026-11/README.md](documentation/review-2026-11/README.md); where the two orderings differ, the review plan wins. Phases here are ordered by risk reduction per effort. Acceptance criteria are in italics.

### Phase 0: Repository and tooling hygiene

- [x] Strict ruff rule set (`E,W,F,I,B,UP,S,ASYNC,N,D,ANN,RUF,PL,PT,SIM,T20,ERA,TRY,FAST,C4,DTZ,EM,LOG,G,PTH,RET,ARG,TC,BLE,FBT,A,ISC,PIE,PERF,FURB,SLF,TID,ICN,INP,PGH`) with `target-version = "py314"` and reasoned ignores in `[tool.ruff]` in `pyproject.toml`. `uv run ruff check` finds nothing.
- [x] `mypy --strict` for `src` and `tests`. `pyright` strict is the second type gate; `ty` is informational only ([ADR 0011](documentation/adr/0011-type-checking-mypy-and-pyright-strict.md)).
- [x] Test and lint tools live in the PEP 735 groups `test` and `lint`; `dev` includes both. *The production image contains no test tooling.*
- [x] Packaging: `uv_build` with a `src` layout; `[project].version` is the only version source (`APP_VERSION`).
- [x] `.python-version` (3.14), `requires-python = ">=3.14"`, `python:3.14-slim` base image.
- [ ] `.env.example`. Partly done: `.env` is kept as the tracked template and `.gitignore` covers `htmlcov/`, `.coverage` and `*.egg-info/`.
- [x] `compose.dev.yaml` with Postgres 18 and Redis 7 for local development.
- [ ] A task runner (`justfile` or `Makefile`): `install`, `dev`, `test`, `lint`, `typecheck`, `migrate`, `build`.
- [ ] `pre-commit` (or `prek`) with ruff, mypy, gitleaks and `uv lock --check`.
- [x] The empty template module is gone; modules follow the fixed layout and [modules.md](documentation/modules.md) shows a complete example.
- [x] Repair the local virtualenv (`uv sync --reinstall`); documented in [Commands](#commands).

### Phase 1: Security hardening

- [x] Startup guard: refuse to boot outside `dev`/`unittest` with a default or weak `JWT_SECRET_KEY`, with (admin panel enabled) a default or weak `ADMIN_PASSWORD`/`ADMIN_SECRET_KEY`, and with a default or non-https `FRONTEND_URL`. Partly: `DATABASE_URL` defaults are not checked.
- [ ] JWT claims `iss` and `aud`. Done: algorithm pinned, claims `exp`/`iat`/`nbf`/`sub`/`jti` required, `type=access` verified, `sub` must be a UUID. *RFC 8725 checklist satisfied.*
- [ ] Refresh cookie: `Secure=True` outside `dev`, `SameSite=Strict` where the frontend allows it, `__Secure-` prefix (`__Host-` requires `Path=/` and would widen the cookie's path), explicit `max_age`. The deletion (`_delete_refresh_cookie` in `modules/auth/router.py`) must carry the same attributes.
- [x] Revoke all sessions on password reset and password change; `POST /auth/logout-all`. (Password change keeps the caller's session.)
- [x] Email-change flow: `PUT /users/me/email` creates a pending change, `POST /auth/email/verify` consumes the `CHANGE_EMAIL` token, dedicated template, `EMAIL_CHANGED` audit event; confirming invalidates open password-reset tokens.
- [ ] Rate limiting with `limits` and Redis: per IP on `/auth/*`, per account on login; `429` with `Retry-After` (D4). Partly mitigated: the timed account lock blocks only password login and leaves existing sessions valid, and a per-process admission limiter bounds login bursts; both remain no substitute for rate limits.
- [x] Uniform credential error (`invalid_credentials`) for locked, inactive and unknown accounts; the real reason is logged and audited server-side only.
- [ ] Security headers: HSTS (behind TLS) and `Content-Security-Policy: frame-ancestors 'none'`. Done: `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy` on every response and `Cache-Control: no-store` under `API_PREFIX`; HSTS belongs to the TLS proxy.
- [x] Trusted proxies: `run.py` passes `FORWARDED_ALLOW_IPS` to uvicorn; access and audit logs read the client IP from the ASGI scope.
- [ ] CORS methods and headers from `Settings`. Partly done: origins come from `CORS_ORIGINS` (`*` refused); methods and headers are still `*`.
- [ ] Admin panel: Argon2 hash for the admin password, `token_hash` hidden in the sessions detail view. Done: `ADMIN_ENABLED` (on by default only in `dev`), constant-time compare of the credentials, read-only views, audited actions, CSRF protection.
- [ ] Protect `/metrics` (network policy or bearer token). Done: the `active_refresh_tokens` gauge and its `KEYS` scan are removed.
- [ ] Request limits: `max_body_size`, per-request timeout, `strict_content_type` kept on.
- [x] Pydantic request models with `extra="forbid"`; separate create, update and response schemas (OWASP API3).
- [ ] `SECURITY.md` with disclosure process and supported versions.
- [ ] Email links to the frontend routes (frontend [task 2](../frontend/documentation/backend-contract.md#backend-task-2-email-links-to-the-frontend-routes)). Done: absolute URLs from `FRONTEND_URL` to the two paths from the settings, URL-encoded token. Open: `/en/user/…` once the user's locale is stored, and `/user/einladung?token=` with the invitation flow (task 1).
- [ ] `POST /auth/password/forgot` (frontend [task 3](../frontend/documentation/backend-contract.md#backend-task-3-distinct-error-codes-and-a-consistent-forgot-password-answer)). Done: the answer is always 202, also when enqueueing fails and for an inactive or admin-locked account. Open: equal response times for known and unknown addresses (known issue 23, with rate limiting).

### Phase 2: API contract

- [x] API versioning: all module routers are mounted under `API_PREFIX` (`/api/v1`); `/`, `/health`, `/health/ready` and `/metrics` stay unversioned. Old paths return 404.
- [ ] Align the Swarm stack's Traefik router with `/api/v1`: `api-prod-strip` still strips `/api`, which would hand the app `/v1/...` (frontend [task 4](../frontend/documentation/backend-contract.md#backend-task-4-refresh-cookie-path-behind-the-api-prefix); deploy round).
- [x] Error format decided (D1, [ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md)); one handler set in `src/app/core/errors.py`: validation, auth, named domain errors, unhandled 500 without internals, rendered by the request middleware with `X-Request-ID`, `request_id` and the security and CORS headers.
- [x] Named exceptions in services instead of `(None, "message")` tuples; routers do not catch `Exception`.
- [x] `response_model` (or a return annotation) on every module route; FastAPI serialises Pydantic directly.
- [ ] Pagination helper: cursor-based for lists, bounded `limit` (default 20, max 100), stable ordering.
- [ ] Optional `Idempotency-Key` for POST (Redis-backed, 24 h TTL, 409 on payload mismatch; D9).
- [ ] `ETag`/`If-Match` on mutable resources (412 on mismatch).
- [ ] Health endpoints: Done: `GET /health` (liveness) and `GET /health/ready` (503 on a failed check); the `HEALTHCHECK` uses `/health`. Open: 503 during shutdown, `GET /health/startup`, the Swarm probes.
- [ ] OpenAPI: `summary`, `description` and `responses` per route (done, guarded by `tests/unit/core/test_openapi_contract.py` and the snapshot); still open: response examples, the CI export, a lint (Spectral or vacuum) and a diff against the previous release (oasdiff).
- [x] Timezone-aware timestamps everywhere (`utcnow()`, ruff `DTZ`), RFC 3339 output.
- [ ] Invitation endpoints `POST /auth/invitations/lookup` and `POST /auth/invitations/accept` (frontend [task 1](../frontend/documentation/backend-contract.md#backend-task-1-invitation-endpoints-new)): tokens only in request bodies, stored as SHA-256 hashes, single use and expiring; the email is bound to the invitation; the accepted user is created verified and joined to the workspace in one transaction; rate limiting, no enumeration, audit events.
- [ ] Distinct error codes for the token endpoints: `token_invalid`, `token_expired`, `token_used` (frontend [task 3](../frontend/documentation/backend-contract.md#backend-task-3-distinct-error-codes-and-a-consistent-forgot-password-answer)). Partly done: `email_already_verified` ships; all token failures are still one `invalid_token`.

### Phase 3: Reliability and observability

- [x] Pure ASGI middleware instead of `BaseHTTPMiddleware`: one `RequestMiddleware` ([ADR 0005](documentation/adr/0005-single-asgi-request-middleware.md)).
- [ ] Tracing: FastAPI native OpenTelemetry (`FastAPI(telemetry=...)`, 0.142+; currently switched off in `create_app()`) plus SQLAlchemy, psycopg, redis and httpx instrumentation; OTLP to Alloy and Tempo; W3C `traceparent`; `trace_id` and `span_id` in log records.
- [ ] Logs: hash or drop client IPs per policy (IPs are still logged); `opentelemetry-instrumentation-logging`. Done: no emails, usernames or query strings in `http.access`.
- [ ] Metrics: align with OTel HTTP semantic conventions (`http.server.request.duration`, `http.route`); add cache hit/miss, task queue depth and DB pool usage.
- [ ] Graceful shutdown: `--timeout-graceful-shutdown`, readiness flips to 503 on SIGTERM, Swarm `stop_grace_period`.
- [x] Settings as an explicit dependency (`get_settings()` cached, `SettingsDep`); importing a module reads no settings and opens no connection.
- [x] `fastapi run` / `fastapi dev` through `[tool.fastapi] entrypoint`; `run.py` is the launcher.
- [ ] Alerting rules in Prometheus (error rate, p95 latency, pool exhaustion, brute force) and a provisioned API dashboard.

### Phase 4: Data layer

- [ ] Unit of work: crud functions stop committing; the service owns the transaction. Registration becomes atomic. (New modules already follow this: see [modules.md](documentation/modules.md).)
- [x] Base model mixins in the core: `UUIDPrimaryKey`, `CreatedAt`, `Timestamps` (server defaults). No `version` column and no soft delete until a module needs them (owner decision 2026-11-22).
- [x] UUIDv7 primary keys, generated in Python (D10, [ADR 0012](documentation/adr/0012-schema-conventions-uuidv7-value-enums-usernames.md)).
- [x] Enums as VARCHAR with a CHECK constraint on the values (`value_enum`); usernames unique regardless of case.
- [x] Absolute session (family) lifetime, `SESSION_MAX_LIFETIME_DAYS`.
- [x] Session cleanup job: `auth.cleanup` at 03:00 UTC deletes ended sessions and closed email tokens after `AUTH_CLEANUP_RETENTION_DAYS`, in batches.
- [x] SQLAlchemy naming convention for constraints; one reviewed baseline revision; a test runs the migrations up, down and up and `alembic check`.
- [ ] `alembic check` in CI (D5); migration review checklist (expand/contract, `CONCURRENTLY`).
- [x] SQLAlchemy 2.1 (`sqlalchemy[asyncio]`, psycopg 3).

### Phase 5: Supply chain and CI/CD

- [ ] CI pipeline (platform to be decided, D5): `uv lock --check`, ruff, mypy, pyright, pytest with the coverage gate, `uv audit`, gitleaks, semgrep, Docker build, Trivy and Grype, SBOM (CycloneDX) attached to the image, SLSA provenance.
- [ ] Dependabot or Renovate for `uv.lock`, Docker base images and CI actions (pin actions by SHA).
- [ ] Conventional Commits enforced; automated changelog and semantic version tags.
- [ ] Mutation testing (mutmut) on `auth/service.py` nightly; Schemathesis against the OpenAPI schema.

### Phase 6: Deployment hardening

- [ ] Dockerfile: `COPY --from=ghcr.io/astral-sh/uv:<pinned>`, cache mounts, `UV_COMPILE_BYTECODE=1`, digest-pinned `python:3.14-slim`, `tini` or exec-form start, read-only root file system with `tmpfs`, healthcheck without external dependencies.
- [ ] Swarm stack: Docker secrets for all credentials, `migrate` with `restart_policy: condition: none`, Traefik with TLS, HTTP to HTTPS redirect, rate-limit middleware, no public `/docs`, `/openapi.json`, `/metrics`, `/admin`, `/health/ready`; resource limits reviewed; `stop_grace_period`; Postgres 18.
- [ ] Monitoring stack: pinned image tags, Grafana admin password from a secret, cAdvisor without `privileged` where possible, Prometheus scraping Swarm service DNS instead of `host.docker.internal`.
- [ ] Kubernetes manifests or a Helm chart as an optional second target (probes, PDB, HPA).

### Phase 7: Documentation and compliance

- [x] `documentation/*.md` match the code (2026-11 cleanup, step S3).
- [ ] `CHANGELOG.md`, `CODEOWNERS`, `CONTRIBUTING.md`, `LICENSE` (D8).
- [ ] Compliance appendix: EU Cyber Resilience Act (SBOM, vulnerability handling, 24 h and 72 h reporting duties active since 2026-09-11), NIS2 supply-chain clauses, ISO 27001:2022 controls A.8.15, 8.16, 8.25, 8.28, 8.29, GDPR log retention policy.
- [ ] Load test baseline (k6) and documented SLOs.

## Known issues

The numbers are stable: other documents and ADRs refer to them. Closed issues stay in the list. The authoritative backlog, including findings that are not listed here, is [documentation/review-2026-11/README.md](documentation/review-2026-11/README.md). The Roadmap phase or round of each open issue is in brackets.

**Closed**

- ~~1~~ The test harness uses the Redis Testcontainer.
- ~~2~~ The test schema comes from Alembic only (no `create_all`), and fixtures and assertions use one sync psycopg engine, so no `asyncio.run` shares the application's engine (`tests/integration/conftest.py`).
- ~~3~~ `/metrics` no longer scans Redis; the `active_refresh_tokens` gauge is removed.
- ~~4~~ `/metrics` is served at the exact path and never counted as a request.
- ~~5~~ Password reset and password change revoke sessions.
- ~~6~~ The email-change token is consumed by `POST /auth/email/verify`; `PUT /users/me/email` starts a pending change, and a dedicated template goes to the new address.
- ~~7~~ The password-reset email link is absolute (`FRONTEND_URL` + `FRONTEND_RESET_PASSWORD_PATH`), and every token link URL-encodes the token.
- ~~9~~ The coverage threshold exists once: `fail_under` in `[tool.coverage.report]`. `addopts` sets no `--cov-fail-under`.
- ~~10~~ No unused settings remain, and the `MAIL_*` values are `Settings` fields (`config.py`). `REDIS_URL`, `BCRYPT_ROUNDS`, `CACHE_KEY_PREFIX`, `ROUTE_CACHE_*`, `DEBUG` and `OTLP_*` are gone.
- ~~11~~ `alembic/env.py` never overrides an injected `DATABASE_URL`. With `ENVIRONMENT=dev` it fills a missing one from `.env.local`; the owner kept that convenience (2026-11-22). The log line `Using DATABASE_URL … (from environment|.env.local)` names the source, so a shell with a global `ENVIRONMENT=dev` cannot silently migrate the wrong database.
- ~~12~~ The `Dockerfile` healthcheck targets `/health`, which works with `METRICS_ENABLED=false`.
- ~~13~~ `ruff format --check` passes.
- ~~14~~ Not reproducible: the virtualenv runs Python 3.14.
- ~~15~~ Admin CSRF: sqladmin runs custom actions as `GET` requests. The admin session cookie is `SameSite=Strict`, `HttpOnly`, `Secure` outside `dev`/`unittest`, and expires 8 hours after login. A fail-safe Fetch Metadata guard answers 403 to every state-changing admin request that does not carry `Sec-Fetch-Site: same-origin`, a missing header included ([OWASP CSRF cheat sheet](https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html)). Browsers without the header (before the March 2023 baseline) cannot log in to the admin or run actions.
- ~~26~~ Reuse detection no longer ends before the session family does. A login's family has an absolute end, `SESSION_MAX_LIFETIME_DAYS` (90) after the login; no rotated session expires after it, and the cleanup deletes a family's sessions only `AUTH_CLEANUP_RETENTION_DAYS` after that end. A replayed rotated token is therefore either found (the family is revoked) or belongs to a family that has ended, whose tokens are all refused ([authentication.md](documentation/authentication.md#emails), Cleanup).
- ~~28~~ The `.env` template is a startable dev configuration: every active line holds the `Settings` default, and settings that are unset by default, secrets, `ENVIRONMENT` and `APP_VERSION` are commented out instead of empty or fixed. `tests/unit/core/test_env_template.py` copies it to `.env.local`, builds the app with `ENVIRONMENT=dev` and checks that no setting differs from its default. The Quick start copies it again.
- ~~29~~ The core tests derive the expected tables and task names from `MODULES` (`Module.models`, `Module.tasks`) and check that no table or task exists outside a registered module; the tests of `users` and `auth` name their own tables and tasks. Adding a module changes only the OpenAPI snapshot, an intended contract change ([modules.md](documentation/modules.md#step-13-regenerate-the-openapi-snapshot)).

**Open**

8. The refresh cookie is set without `Secure`. When `Secure` or a name prefix is added, `_delete_refresh_cookie` in `modules/auth/router.py` must send the same name and attributes, or browsers keep the old cookie. Use `__Secure-`, not `__Host-`: `__Host-` requires `Path=/`, which conflicts with the narrow `<API_PREFIX>/auth` path. [Phase 1]
16. The admin action "Force password change" sets `password_credentials.must_change`, but neither login, refresh nor access tokens check it, and a password change or reset does not clear it. [Phase 3]
17. A locked account can be told apart by response time while the Argon2 queue is loaded: a refusal on a locked account waits like an average verify, but does not queue behind other password evaluations. The real control is rate limiting ([D4](#open-decisions)). [Phase 3]
18. **Accepted risk:** `PUT /users/me/email` answers 409 `email_taken` when the new address belongs to another account, so a signed-in user who knows their password can test whether an address is registered. The owner accepted this: `POST /auth/register` reveals the same with its 409, and only the account holder (after the password check) can ask.
19. `/health/ready` is routed publicly by the Swarm stack; it should be reachable only from the orchestrator and monitoring. [deploy round]
20. On a Linux host shared by several users, the worker's default `PROMETHEUS_MULTIPROC_DIR` (`/tmp/taskiq-metrics`) is a predictable path in the shared `/tmp`: another local user can create it first, plant symlinks or alter the metric files. Containers, Windows and macOS (per-user temp directories) are not affected. The deploy round sets an explicit directory on a tmpfs. [deploy round]
21. **Accepted for now:** there is no dead-letter queue and no delivery cap. A message whose worker dies on every attempt (for example a crash after the mail was sent but before the acknowledgement) is redelivered every 10 minutes without end.
22. **Accepted for now:** when the scheduler fails to send a due retry (a short Redis outage during the send), taskiq's scheduler does not try that retry again until the scheduler restarts.
23. `POST /auth/password/forgot` answers an unknown address at once, a known inactive or admin-locked account after an audit row, and a known active account after a token, an audit row and the enqueue: three timing classes, so the response time tells whether an address is registered and roughly in which state. The answer itself is always 202. Likewise, while Redis is unreachable, the login request that sets a timed lock (the fifth wrong password) waits up to the broker's socket timeout (5 s) for the lock notice's enqueue, which shows that the account exists. Like known issue 17, the control is rate limiting ([D4](#open-decisions)); equal timing is not attempted before it. [D4, review plan Phase 4]
24. Retry schedules (`taskiq_retry:*`) never expire: while the scheduler is down they pile up, and the scheduler sends every overdue one when it starts again. For the token mails this is harmless (the schedule holds only the token's id, and a run after the link's expiry sends nothing), and the lock notice checks that the lock is still active; a late email-change notice is still true when it arrives. A future task with time-sensitive effects must check its own deadline. [Phase 3]
25. **Accepted:** a refresh that fails with 401 clears the refresh cookie (same name and path). If that 401 reaches the browser after the response of a login made in parallel, it deletes the new login's cookie, and the user has to sign in again. This affects availability only.
27. **Accepted:** the lock-notice throttle is check, send, record, and not atomic. After a worker backlog several lock notices of one user can go out together, at most one per lock.
30. The Swarm stack sets no `FRONTEND_URL`, `CORS_ORIGINS` or `FORWARDED_ALLOW_IPS`. With `ENVIRONMENT=prod` the startup guard stops the API until `FRONTEND_URL` is a real `https` URL, and the cookie, CORS and client-IP behaviour need the real values. [deploy round]

## Open decisions

Decisions deliberately deferred. A decision that is taken gets an ADR, and its row then links to it. Do not resolve one silently in code.

| # | Topic | Options | Current lean |
|---|---|---|---|
| D1 | Error response format | keep the `success/data/error/metadata` envelope, or RFC 9457 `application/problem+json` | **Decided 2026-11-16:** RFC 9457 for errors, plain typed responses for success, API under `/api/v1`. See [ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md) (implemented) |
| D2 | Admin panel | keep `sqladmin` and harden it, or remove it from the boilerplate | keep, off by default outside `dev` |
| D3 | Authorization model | generic RBAC/permissions tables in the boilerplate, or leave `roles`/`permissions` placeholders | add RBAC as its own phase |
| D4 | Rate limiting placement | in-app (`limits` + Redis) plus Traefik, or gateway only | both; app limits are environment-independent |
| D5 | CI platform | GitHub Actions, GitLab CI, other | undecided |
| D6 | Type checker | mypy strict as the gate with `ty` advisory, or pyright | **Decided 2026-11-22:** mypy strict and pyright strict are both gates, `ty` is informational. See [ADR 0011](documentation/adr/0011-type-checking-mypy-and-pyright-strict.md) |
| D7 | Repository layout | monorepo README at the root plus this file, one AGENTS.md per package | root overview and per-package files (in place) |
| D8 | License and compliance scope | internal, or open source; whether CRA/NIS2 deliverables (SBOM, SECURITY.md, reporting process) are mandatory | treat as mandatory for an industrial product |
| D9 | Idempotency keys | implement in the boilerplate now (the frontend blueprint requires them) or later | Phase 2 |
| D10 | Primary key strategy | UUIDv4, UUIDv7, bigint | **Decided 2026-11-22:** UUIDv7 generated in Python (`uuid.uuid7()`), a module may opt out to UUIDv4 per table. See [ADR 0012](documentation/adr/0012-schema-conventions-uuidv7-value-enums-usernames.md) (implemented) |
| D11 | Deployment target | Docker Swarm (current), Kubernetes, both | Swarm now, Kubernetes manifests later |
| D12 | Password hashing library | keep `argon2-cffi` directly, or `pwdlib` (FastAPI tutorial default, bcrypt upgrade path) | keep `argon2-cffi` |

## Standards and references

- OWASP API Security Top 10 (2023), OWASP ASVS 5.0, OWASP Cheat Sheets: REST Security, Password Storage, JWT
- RFC 8725 (JWT Best Current Practices), RFC 9457 (Problem Details), RFC 9562 (UUIDv7), RFC 9745 (Deprecation header), RFC 8594 (Sunset header)
- OpenTelemetry HTTP semantic conventions; W3C Trace Context
- The Twelve-Factor App and the "Beyond the Twelve-Factor App" additions (API first, telemetry, auth)
- FastAPI docs: Bigger Applications, Dependencies with yield, OpenTelemetry, Deployment with Docker
- Docker build best practices; uv Docker integration guide; SLSA v1.2; CycloneDX 1.7
- EU Cyber Resilience Act (Regulation (EU) 2024/2847); NIS2 (NIS2UmsuCG in Germany); ISO/IEC 27001:2022
