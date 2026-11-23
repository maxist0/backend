# AGENTS.md — backend

Guidance for coding agents (Claude Code, Codex, Cursor, Copilot) and humans working in this repository. This file is the single source for backend conventions. Overview, quick start, configuration, roadmap and known issues are in [README.md](README.md); link to them, do not duplicate them here. Decisions are recorded as ADRs in [documentation/adr/](documentation/adr/README.md). Topic pages are in [documentation/](documentation/).

## Project in one paragraph

A **module boilerplate** on FastAPI: async SQLAlchemy 2 on PostgreSQL 18, Redis (taskiq broker and results, cache; refresh tokens live only in Postgres), taskiq for background jobs, structlog and Prometheus for observability, Docker Swarm for deployment. Python **3.14** is the target. A stable **core** carries user management, scaling and the other production features. Someone building a web app **adds modules and never edits the core**. The repository contains **no business features**. Gaps are in [README → Roadmap](README.md#roadmap) and [Known issues](README.md#known-issues). Decisions not yet taken are in [Open decisions](README.md#open-decisions); do not pre-empt them in code.

## Simplicity first

- Build no machinery for cases that do not exist yet (for example a router mounted under several prefixes). Every abstraction needs a user today.
- Prefer the standard or library mechanism over custom code. Prefer custom code over a new dependency only when it is a few lines.
- Before structural changes, ask clarifying questions first. Agents return a "Clarifying questions" block ([agent standard §2](../.claude/agent-standards.md)).

## Golden rules

1. Async only. Never call sync DB, Redis, SMTP or HTTP from request handlers or tasks.
2. Read before you write: `src/app/core/config.py`, `src/app/core/errors.py`, `src/app/core/__init__.py`, and the module you touch.
3. Every value that could differ per environment comes from `Settings`. No hard-coded URLs, TTLs, limits, origins or secrets.
4. Never commit secrets, `.env.local`, or real credentials. `.env` is a tracked template: non-secret defaults, secrets commented out or empty.
5. Generate migrations, never apply them outside your local database. Never edit an applied migration.
6. No new dependency without stating why and checking it is maintained (see "Do not use").
7. Tests are part of the change. A feature without tests is not done.

## Setup and commands

```bash
uv python install 3.14 && uv sync          # install (creates .venv with dev group)
docker compose -f compose.dev.yaml up -d --wait   # local Postgres and Redis
uv run alembic upgrade head                 # migrate local DB (reads .env.local with ENVIRONMENT=dev, else DATABASE_URL)
uv run python run.py dev                    # API on :8000 + taskiq worker + scheduler, reads .env.local
uv run ruff check src tests run.py alembic   # lint (ruff pinned in the lint group)
uv run ruff format src tests run.py alembic  # format; explicit paths, ruff would also format Markdown code blocks
uv run mypy                                 # types, strict, src + tests; paths come from [tool.mypy] in pyproject.toml
uv run pyright                              # second type gate, strict ([tool.pyright]); the checker behind VS Code's Pylance
uv run ty check                             # informational only (ADR 0011), not a gate
uv run pytest tests/unit --no-cov           # fast loop, no Docker; the coverage gate applies only to the full run
uv run pytest                               # full suite (unit + integration, coverage gate); needs a running Docker daemon
uv run pytest tests/integration -k "refresh" -x
uv audit --frozen                           # known vulnerabilities of the locked dependencies
uv lock --check                             # uv.lock matches pyproject.toml
uv run alembic revision --autogenerate -m "describe_change"   # after model changes; ruff fixes and formats it (alembic.ini hooks), then review the file
docker build -t api:latest .                # image
```

`pytest` adds `--cov` through `addopts` (`pyproject.toml`). A unit-only run cannot reach the coverage threshold, so pass `--no-cov` for it. Use `uv run ...`; never `pip install` or activate a virtualenv by hand. If `.venv` is broken, `uv sync --reinstall`.

## Where things are

The annotated file tree is in [README → Project layout](README.md#project-layout). The public core facade is `src/app/core/__init__.py` (`__all__` is the full list). Deployment files are under `deployment/docker/`.

## Core vs modules

- `src/app/core/` is the infrastructure. `src/app/modules/` holds the modules. Two modules are built in: `users` (the `User` entity, profile, later settings) and `auth` (how a user proves identity: credentials, sessions, tokens, audit log). See [ADR 0008](documentation/adr/0008-module-layout-and-users-auth-split.md).
- **Imports point one way: `core ← users ← auth ← app modules`.** The core never imports from `app.modules`; `users` never imports `auth`; a module never imports a module that depends on it. Modules import infrastructure from `app.core`, and `CurrentUser`, `OptionalUser`, `User` from `app.modules.users`.
- **URLs follow the resource, code follows the responsibility.** Endpoints for the signed-in user live under `/users/me/...`; flows for signed-out users live under `/auth/...`. `PUT /users/me/password` and `PUT /users/me/email` need credentials and verification, so the `auth` module serves them (OpenAPI tag `users`).
- `app.core` is a facade. Modules import only from `app.core`, never from its submodules; `tests/unit/core/test_import_direction.py` enforces it. Importing `app.core` reads no settings and opens no connection.
- An app built on this boilerplate adds modules. If a task seems to need a core edit, stop and ask: either the core has a gap (a core change with an ADR) or the code belongs in a module.

## Module layout

Every module uses the same file names, and only these. A module creates the files it needs; it never adds other names. Code that fits none of them is infrastructure (core) or a sign the module should be split. `tests/unit/core/test_module_layout.py` enforces the vocabulary.

```
src/app/modules/<name>/
  __init__.py       module = Module(...) and the module's public exports
  router.py         HTTP routes: thin, typed, one service call each
  schemas.py        Pydantic request/response models
  service.py        business logic; owns the transaction
  crud.py           database queries; no business rules, never commits
  models.py         SQLAlchemy models
  dependencies.py   FastAPI dependencies
  errors.py         AppError subclasses with the module's error codes
  tasks.py          taskiq tasks (`@task`)
  admin.py          sqladmin views
  templates/        email templates (never named `base.html`; that is the core layout)
```

## Adding a module

Step by step with a complete example: [documentation/modules.md](documentation/modules.md). The rules:

- `__init__.py` defines `module = Module(name=..., router=..., models=..., tasks=..., admin_views=...)`. `Module` is a frozen dataclass; `models`, `tasks` and `admin_views` are optional. The router carries its own `prefix` and `tags`.
- Register it in `MODULES` in `src/app/modules/__init__.py`, after the modules it depends on. There is no auto-discovery. `create_app()` mounts every module router under `settings.API_PREFIX` (default `/api/v1`); the worker imports `module.tasks`, Alembic imports `module.models`, the admin registers `module.admin_views`.
- Autogenerate a migration, review it, apply it locally only.
- Tests go under `tests/unit/modules/<name>/` and `tests/integration/modules/<name>/`. Module-specific fixtures go into a `conftest.py` there; never edit the root `conftest.py`.

## Architecture rules

- Layering inside a module: `router → service → crud → models`. Routers never query the DB; services never build HTTP responses; crud contains no business rules. Create only the files the module needs.
- The service owns the transaction (unit of work). crud never calls `commit()` or `rollback()`; it may `flush()` to get generated values or to raise an `IntegrityError` where the service maps it. The service ends each use case with one `await db.commit()` and enqueues tasks after it. A second commit inside a use case needs a comment saying why: writes that a following error would roll back (failed-login counters, audit rows), a row lock that must not last over Argon2 or an enqueue, batches. `get_db` never commits. `tests/unit/core/test_crud_transactions.py` enforces the crud rule for every module.
- Routers are thin: validate via Pydantic, call one service function, return a typed response. Every route declares `response_model` (or a return annotation), `status_code`, `summary` and the error `responses` it can produce.
- Dependencies use `Annotated`: `db: DbSession`, `user: CurrentUser`. Never instantiate `AsyncSession` yourself. Use `Depends(..., scope="function")` only when cleanup must happen before the response is sent.
- Lifespan is the only startup/shutdown mechanism. No `on_event`, no work at import time.
- No shared base classes for crud/service; keep modules explicit and standalone.
- User hooks for modules are the `CurrentUser`/`OptionalUser` aliases only. Roles and permissions (D3), user events and per-module settings are deferred; do not build them. The 2FA stubs stay.

## Errors and responses

- Routes return typed Pydantic models directly. There is no envelope.
- Errors are RFC 9457 `application/problem+json`: `type` (`about:blank` or a docs URL), `title`, `status`, `detail`, a stable machine-readable `code` and `request_id`. A 422 adds `errors[]` with `field`, `code` and `message`. See [ADR 0002](documentation/adr/0002-rfc9457-errors-plain-responses-api-v1.md).
- Services raise `AppError` subclasses: `BadRequest`, `Unauthorized` (sets `WWW-Authenticate`), `Forbidden`, `NotFound`, `Conflict`. A module's errors subclass them and set their own `code`.
- No `(value, err)` tuple returns. No `try/except Exception` in routes. No `start_time` or `request` plumbing through services.
- The API is versioned under `/api/v1`. `/health`, `/health/ready` and `/metrics` stay unprefixed.
- REST nouns, plural, kebab-case paths; actions as sub-resources (`/auth/password/reset`).
- Status codes: 200 read/update, 201 create, 204 delete, 400 bad input, 401 missing or invalid credentials, 403 authenticated but not allowed, 404, 409 conflict, 422 validation, 429 rate limited, 500 never with internals.
- Timestamps RFC 3339 in UTC; IDs are UUID strings.
- Never expose stack traces, SQL or internal identifiers in responses.

## Code style

- Ruff for lint and format, mypy and pyright for types (both strict, `src` and `tests`). All code must pass all three. A suppression names its rule and gives the reason on the same line: `# type: ignore[code]  # reason`, `# pyright: ignore[rule] - reason`, `# noqa: CODE - reason`. No `Optional[X]` (use `X | None`), no `List`/`Dict`/`Tuple` from typing, no naive `datetime.now()` (use `utcnow()` from `app.core`), no `print`, no commented-out code, no `assert` outside tests.
- Type every parameter and return value. `Any` needs a comment explaining why.
- Pydantic v2 only: `model_config = ConfigDict(...)`, `field_validator`, `model_validator`, `model_dump(mode="json")`. Request models set `extra="forbid"`. Separate create, update and response models, named `CreateXRequest`, `UpdateXRequest`, `XResponse`; never return ORM objects.
- Naming: `snake_case` functions and modules, `PascalCase` classes, `UPPER_SNAKE` constants, schemas suffixed `Request`/`Response`, exceptions suffixed `Error`.
- Structured log events, not messages: `logger.info("auth.login_success", user_id=str(user.id))`. Event names are `area.action`.
- Imports are absolute: `from app.core import DbSession`, `from app.modules.users import CurrentUser`. No circular imports.

## Comments and docstrings

House style, enforced by ruff (rule sets `D` with the google convention, and `ERA`).

- English throughout. Google-style docstrings (`Args:`, `Returns:`, `Raises:`) with single backticks. An example is `token_link` in `src/app/modules/auth/tasks.py`.
- Docstrings are required on public functions and classes in `core/` and on service functions. Route docstrings are user-facing: they render in Swagger.
- An inline comment explains only a WHY the code cannot express: a security invariant, a lock order, a library quirk with a source link, or a non-obvious bound.
- Forbidden:
  - file-path header comments (`# src/app/...`);
  - banner and section separators (`# -----`);
  - comments that restate the code;
  - review, phase or finding IDs in code (`Phase 0 fix 6`, `review 2026-11`, `NEW-3`, `BE-10`). They belong in commit messages and ADRs;
  - TODOs without an issue reference;
  - commented-out code;
  - legacy typing (`Optional`, `Dict`, `List`, `Tuple`).

## Security rules

- Passwords: `argon2-cffi` via `hash_password`/`verify_password` in `src/app/core/security.py`. Never store or log a plaintext password or token; tokens at rest are SHA-256 (`hash_token`).
- JWT via `PyJWT` with an explicit `algorithms=[...]` list (HS256, pinned in `src/app/core/security.py`). Never read the algorithm from the token.
- Refresh tokens live only in Postgres; the refresh gate is one atomic `UPDATE ... RETURNING`. Do not add a Redis mirror.
- Secrets are `SecretStr` in `Settings`; a startup guard rejects defaults outside `dev`/`unittest`. New secrets need a guard entry and a README configuration row.
- Auth-protected routes use `CurrentUser` (or `OptionalUser`); do not re-implement token parsing. Do not change `src/app/modules/auth/`, `src/app/modules/users/` or `src/app/core/security.py` internals unless the task is about auth.
- Error messages must not reveal whether an account exists (see [Known issues](README.md#known-issues) for the accepted gaps).
- Validate and bound every input (`max_length`, ranges, enums). Reject unknown fields.
- Routes that send mail, check a secret or create accounts get a rate limit: `rate_limit(...)` on the route or `hit(...)` in the service before the lookup or the hash ([ADR 0013](documentation/adr/0013-rate-limiting-in-the-app-on-redis.md), [modules.md](documentation/modules.md#rate-limiting-a-route)). A 429 must not tell more than the normal answer would, so count submitted identifiers before any lookup.
- CORS, cookies and headers are configured centrally (`create_app()` and `src/app/core/middleware.py`); do not set them per route. The client IP comes only from the ASGI scope; uvicorn's proxy headers are enabled with trusted `FORWARDED_ALLOW_IPS`.
- Do not log request bodies, query strings, `Authorization`, cookies, emails or usernames. User context in logs is `user_id` only.
- Log unexpected exceptions with `log_unexpected_error(logger, event, exc)` or `error_type=type(exc).__name__`, never `error=str(e)`: database error text can contain user data.

## Database and migrations

- SQLAlchemy 2 style only: `Mapped[...]`, `mapped_column`, `select()`; no legacy `Query`. The driver is psycopg 3.
- Models live in `src/app/modules/<name>/models.py` and subclass `Base` from `app.core`. Alembic collects them through `MODULES`.
- Avoid lazy loads in async code: use `selectinload`/`joinedload` or `await session.refresh(obj, [...])`.
- `expire_on_commit=False` is set; still re-read objects you need after a commit if another transaction may have changed them.
- Model change → autogenerate → read the generated file, fix types and names, add indexes → `uv run alembic upgrade head` locally only. Downgrade must work. Prefer expand/contract for data migrations.
- New tables use the core mixins: `UUIDPrimaryKey` (UUIDv7 generated in Python), `Timestamps` (`created_at`/`updated_at`, `timestamptz`, server default `now()`) or `CreatedAt` for rows that never change. Explicit indexes for every FK and filter column. See [database.md → Model conventions](documentation/database.md#model-conventions) and [ADR 0012](documentation/adr/0012-schema-conventions-uuidv7-value-enums-usernames.md).
- Enum columns use `value_enum(MyEnum)` from `app.core`: a VARCHAR that stores the values (`login_success`) with a CHECK constraint. Never a native PostgreSQL enum. A changed value needs a migration that replaces the CHECK constraint; autogenerate does not see it.
- Constraint names come from the naming convention on `Base.metadata`. Code that reacts to a violation compares `constraint_name(exc)` with a named constant, never with a PostgreSQL default name.
- Case-insensitive uniqueness is a unique index on `lower(column)`; queries filter on the same expression. No `citext`.

## Caching and background jobs

- The core offers `@cached(ttl, key)` and `invalidate(pattern)`: a thin cashews wrapper that caches JSON return values on the one Redis client ([ADR 0003](documentation/adr/0003-caching-via-cached-decorator.md)). Cache keys include every input that changes the result, `{user_id}` included for per-user data; a missing component leaks data across users. Mutations call `invalidate(...)` in the service. TTLs come from `Settings`. There is no ETag middleware, no client cache-control middleware and no route-cache settings.
- All background work is a taskiq task in the module's `tasks.py`, declared with `@task(task_name=...)` from `app.core`, exported through `Module(tasks=...)` and enqueued with `await my_task.kiq(...)` after the commit. Do not use FastAPI `BackgroundTasks`. Importing a module must read no settings: the broker is built by `app.worker` and by the API lifespan.
- Tasks receive IDs and primitives, never ORM objects, and never personal data or secrets: task arguments are stored in Redis and logged by taskiq at DEBUG. Pass the id of the row the task works on and let the task read the rest ([ADR 0010](documentation/adr/0010-token-emails-rekeyed-in-the-worker.md)).
- A task gets its database session with `db: TaskDbSession = TaskiqDepends()` (`TaskiqDepends` from `taskiq`). One session per run; the task's service owns the transaction. Do not catch database errors in a task to log their text: the `task` decorator already replaces them by `TaskDatabaseError` with types and SQLSTATE only.
- Delivery is **at least once** ([ADR 0009](documentation/adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md)): a task can run twice, so every task is idempotent.
- Retries: a task with the label `retry_on_error=True` that raises `TransientTaskError` runs again after `TASKIQ_RETRY_DELAY_SECONDS`, at most `TASKIQ_RETRY_COUNT` times. Raise it only for errors that may pass (network, timeouts, a busy server). Its message must hold no personal data. Mail tasks raise `mail_delivery_error(exc) from None`. Delayed retries are schedules that the scheduler sends: a worker without a scheduler never retries (`run.py dev` starts both).
- Periodic tasks use the label `schedule=[{"cron": "0 3 * * *"}]`; times are UTC on every host, so do not set `cron_offset`. Exactly one scheduler runs, but a periodic task must still be idempotent. Batch large deletes (one transaction per batch, `FOR UPDATE SKIP LOCKED`, no user lock), as `auth.cleanup` does.
- The retry and Prometheus middlewares belong to the worker (`app.worker`) only. Building the worker broker in an API process would put the API's `/metrics` into prometheus_client's multiprocess mode.

## Testing

- `tests/unit/` needs no Docker and runs in seconds. If the code under test needs no DB or Redis, it is a unit test. `tests/integration/` uses `TestClient` with Postgres and Redis Testcontainers. Do not mock the DB. Patch only external side effects: the `captured_emails` fixture replaces the mailer. ADR: [0007](documentation/adr/0007-test-split-unit-integration.md).
- Tasks run in-process in integration tests (the `app` fixture's broker is taskiq's `InMemoryBroker`), so a failing task fails the test. `run_task(client, task, *args)` runs one outside a request. Tests of the Redis transport use `redis_broker_client`. Details: [documentation/testing.md](documentation/testing.md).
- Layout: `tests/unit/{core,modules/<name>}` and `tests/integration/{core,contract,modules/<name>,admin}`. There is no backend e2e folder; frontend Playwright covers journeys.
- Do not test third-party libraries. Guard our own configuration of them.
- Each new endpoint gets: happy path, validation error, auth failure (missing, invalid, expired token), not-found or conflict, and one test proving the side effect (audit row, email, cache invalidation).
- Tests are independent: the autouse `clean_db` fixture truncates the application tables **before** each test. Use the helpers in `tests/helpers.py` (`register`, `login`, `bearer`, `Db`, `log_events`, ...); add a shared helper there instead of a copy in a test module.
- Every test must fail without the change it covers. Keep the suite deterministic (no sleeps, no real network).
- The coverage threshold is enforced by pytest-cov on the full run; do not lower it to make a PR pass.

## Definition of done

- [ ] `uv run ruff check src tests run.py alembic` and `uv run ruff format --check src tests run.py alembic` pass
- [ ] `uv run mypy` and `uv run pyright` pass
- [ ] The OpenAPI snapshot test passes; after an intended contract change, regenerate `tests/unit/core/openapi_snapshot.json` with `UPDATE_OPENAPI_SNAPSHOT=1` and review the diff
- [ ] `uv run pytest` passes locally with Docker running
- [ ] `uv audit --frozen` and `uv lock --check` pass
- [ ] New settings added to `src/app/core/config.py`, the `.env` template and the README configuration table
- [ ] Migration generated and reviewed if models changed; command listed in the PR description
- [ ] README sections touched by the change are updated in the same PR; no TODOs without an issue reference; no new `# type: ignore` without a reason

## Git and pull requests

- Conventional Commits: `feat(auth): rotate refresh token family on reuse`, `fix(cache): ...`, `docs: ...`. One logical change per commit; keep `uv.lock` churn out of unrelated commits.
- PR description: what, why, how to verify, migration and config notes, roadmap item reference.
- Agents do not commit, push, tag or open PRs unless explicitly asked in the task.

## Boundaries

**Always:** run lint, types and tests before reporting done; keep changes minimal and local; update docs with code; ask when a decision in README → Open decisions is needed.

**Ask first:** adding or upgrading a dependency; changing `pyproject.toml` tooling config, `Dockerfile`, anything under `deployment/`; changing the middleware, security headers, CORS or cookie settings; any edit to `src/app/core/` (see "Core vs modules"); cross-module refactors; deleting files; touching `alembic/env.py`.

**Never:** apply migrations against a non-local database; run `docker stack deploy`, `docker stack rm` or anything against a Swarm manager; commit secrets or `.env.local`; disable or weaken a security check, a lint rule or a test to make something pass; log credentials, tokens or PII; use `python-jose`, `passlib`, `fastapi-cache2`, `arq`, or sync DB drivers; add business-domain features to this boilerplate; import from `app.modules` inside `app.core`.

## Do not use

`python-jose` (unpatched critical CVE), `passlib` (unmaintained, breaks on bcrypt ≥ 4.1), `fastapi-cache2` (dormant), `arq` (maintenance only), Celery for async code, `requests` in async paths (use `httpx`), `datetime.utcnow()`, `ORJSONResponse` (deprecated in FastAPI 0.131, declare a `response_model` instead).
