# General structure

How the backend is assembled, how a request travels through it and which process runs what. For the file tree, see [README → Project layout](../README.md#project-layout). For the rules, see [AGENTS.md](../AGENTS.md). To add a feature, see [modules.md](modules.md).

Form: explanation, with reference tables.

## Core and modules

`src/app/core/` is the infrastructure: settings, database, errors, security, middleware, health, cache, tasks, mail, logging and the admin base. `src/app/modules/` holds the features. Imports point one way:

```
core ← users ← auth ← your modules
```

- A module imports infrastructure from `app.core` only, never from its submodules. `tests/unit/core/test_import_direction.py` enforces it.
- `users` never imports `auth`. A module never imports a module that depends on it.
- The core never imports from `app.modules`.

A module is a package with a `module = Module(...)` object (`src/app/core/module.py`). `MODULES` in `src/app/modules/__init__.py` lists the modules in dependency order. Four composition roots read that tuple:

| Root | What it takes from each module |
|---|---|
| `create_app()` in `src/app/main.py` | `router` (mounted under `API_PREFIX`) and `admin_views` |
| `src/app/worker.py` (worker and scheduler) | `tasks` (importing `app.modules` registers every task) |
| `alembic/env.py` | `models` (importing `app.modules` fills `Base.metadata`) |
| the admin panel (`create_admin`) | `admin_views` |

Importing `app.core` or `app.modules` reads no settings and opens no connection. A unit test starts a fresh interpreter without `ENVIRONMENT` to prove it (`tests/unit/core/test_model_registry.py`). The decisions are [ADR 0001](adr/0001-core-and-module-contract.md) and [ADR 0008](adr/0008-module-layout-and-users-auth-split.md).

## Runtime topology

```
                         ┌──────────────┐
   Internet ──HTTPS──▶   │   Traefik    │  TLS termination, routing
                         └──────┬───────┘
                                │ HTTP
            ┌───────────────────┼────────────────────┐
            ▼                   ▼                    ▼
     ┌────────────┐      ┌────────────┐       ┌────────────┐
     │  api       │      │  api       │       │  api       │   uvicorn, one process each
     └─────┬──────┘      └─────┬──────┘       └─────┬──────┘
           │  psycopg 3        │  redis-py          │
           ▼                   ▼                    ▼
     ┌────────────┐      ┌────────────┐       ┌──────────────────┐
     │ PostgreSQL │      │   Redis    │◀──────│ taskiq worker(s) │  tasks
     │            │      │ db0 cache  │       │ taskiq scheduler │  cron, retries
     └────────────┘      │ db1 stream │       └──────────────────┘
                         │ db2 results│
                         └────────────┘
           metrics at /metrics, logs on stdout (JSON)
                         ▼
        Prometheus ── Grafana ── Loki ── Tempo ── Alloy
```

The Swarm stack in `deployment/` runs three API replicas, two workers and one scheduler. Run exactly one scheduler. Each API process and each worker process has its own database pool (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`).

`run.py dev` starts one API process, the worker and the scheduler on your machine ([README → Quick start](../README.md#quick-start)).

## Lifespan

`create_app()` assembles the app without I/O: error handlers, middleware, health and metrics routes, the routers of `MODULES` and, when enabled, the admin panel. The lifespan in `src/app/main.py` then opens the resources in this order and closes them in reverse order, also when a later step fails:

1. Logging (`setup_logging`).
2. Event-loop check (psycopg needs a `SelectorEventLoop` on Windows), database engine, connectivity check `SELECT 1`, binding of the session factory.
3. The Redis client (`RedisDep`) and a ping.
4. The cache (`@cached`) on the same Redis server.
5. The taskiq broker, which the API only uses to enqueue.

Lifespan is the only startup and shutdown mechanism. There is no `on_event` and no work at import time.

## Request lifecycle

The last middleware added runs first. For a request the effective order is:

1. Starlette's `ServerErrorMiddleware`, the last resort for a failure in the middleware itself.
2. `CORSMiddleware` with `CORS_ORIGINS`. It answers preflights itself.
3. `RequestMiddleware` (`src/app/core/middleware.py`, pure ASGI, [ADR 0005](adr/0005-single-asgi-request-middleware.md)):
   - validates the client's `X-Request-ID` (1 to 128 characters of `A-Za-z0-9._-`) or generates one, binds it to the structlog context and returns it as `X-Request-ID`;
   - sets `X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy` and `Permissions-Policy`, and `Cache-Control: no-store` under `API_PREFIX` unless the route set one;
   - records `http_requests_total` and `http_request_duration_seconds` (never for `/metrics`) and writes one `http.access` event;
   - logs an unhandled exception with `log_unexpected_error` and answers with the generic 500 problem, so the 500 carries the same headers and a `request_id`.
4. Router, then dependencies (`DbSession`, `CurrentUser`), the route function, the service and crud.
5. Exception handlers turn `AppError`, `RequestValidationError` and Starlette `HTTPException` into RFC 9457 problem responses ([api.md](api.md)).

## Layers in a module

```
router → service → crud → models
```

- The router validates input through Pydantic, calls one service function and returns a typed model.
- The service holds the business rules and owns the transaction. It enqueues tasks after the commit.
- crud holds the queries and no rules.
- Routers never query the database. Services never build HTTP responses.

## Environments

`ENVIRONMENT` selects behaviour; the table is in [README → Configuration](../README.md#configuration) and the details in [environment.md](environment.md). `run.py` takes the mode from its argument (`dev` or `unittest`) or from an injected `ENVIRONMENT`.

## Migrations

Alembic uses an async `env.py` that reads `DATABASE_URL` only and builds no application settings, so a migration job needs no application secrets. Details: [database.md](database.md).

## Related pages

- [api.md](api.md): routes and the error format.
- [authentication.md](authentication.md): the identity flows.
- [redis.md](redis.md): cache, broker and scheduler.
- [logging.md](logging.md), [monitoring.md](monitoring.md): logs, metrics, health.
