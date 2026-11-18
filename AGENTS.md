# AGENTS.md — backend

Guidance for coding agents (Claude Code, Codex, Cursor, Copilot) and humans working in this repository. This file is the single source for backend conventions. Human-facing docs, architecture and the roadmap are in [README.md](README.md); link to them, do not duplicate them here. Decisions are recorded as ADRs in [documentation/adr/](documentation/adr/README.md).

> **Target state.** Sections marked *(target)* describe the layout and rules decided on 2026-11-16 and implemented in steps S1–S3 of the 2026-11 cleanup (S1 structure, S2 taskiq reliability, S3 style pass, strict ruff/mypy, docs). S0 and S1 are done; the remaining steps (frontend migration, S2, S3) and the working rules for them are in [documentation/review-2026-11/cleanup-plan.md](documentation/review-2026-11/cleanup-plan.md). New code follows the target.

## Project in one paragraph

A **module boilerplate** on FastAPI: async SQLAlchemy 2 on PostgreSQL 18, Redis (taskiq broker and results, cache; refresh tokens live only in Postgres), taskiq for background jobs, structlog and Prometheus for observability, Docker Swarm for deployment. Python **3.14** is the target. A stable **core** carries user management, scaling and the other production features. Someone building a web app **adds modules and never edits the core**. The repository contains **no business features**. Gaps and the plan are in [README.md → Roadmap](README.md#roadmap) and [Known issues](README.md#known-issues). Decisions not yet taken are in [Open decisions](README.md#open-decisions); do not pre-empt them in code.

## Simplicity first

- Build no machinery for cases that do not exist yet (for example a router mounted under several prefixes).
- Every abstraction needs a user today.
- Prefer the standard or library mechanism over custom code. Prefer custom code over a new dependency only when it is a few lines.
- Before structural changes, ask clarifying questions first. Agents return a "Clarifying questions" block ([agent standard §2](../.claude/agent-standards.md)).

## Golden rules

1. Async only. Never call sync DB, Redis, SMTP or HTTP from request handlers or tasks.
2. Read before you write: `src/app/core/config.py`, `src/app/core/errors.py`, `src/app/core/__init__.py`, and the module you touch.
3. Every value that could differ per environment comes from `Settings`. No hard-coded URLs, TTLs, limits, origins or secrets.
4. Never commit secrets, `.env.local`, or real credentials. `.env` is a template with empty values.
5. Generate migrations, never apply them outside your local database. Never edit an applied migration.
6. No new dependency without stating why and checking it is maintained (see "Do not use").
7. Tests are part of the change. A feature without tests is not done.

## Setup and commands

```bash
uv python install 3.14 && uv sync          # install (creates .venv with dev group)
uv run alembic upgrade head                 # migrate local DB (reads .env.local)
uv run python run.py dev                    # API on :8000 + taskiq worker + scheduler, reads .env.local
uvx ruff@0.15.0 check src tests             # lint (S3 switches to `uv run ruff`, lint group)
uvx ruff@0.15.0 format src tests            # format
uv run mypy src                             # types (strict mode arrives in S3)
uv run pytest tests/unit --no-cov           # fast loop, no Docker; the coverage gate applies only to the full run
uv run pytest                               # full suite (unit + integration, coverage gate); needs a running Docker daemon
uv run pytest tests/integration -k "refresh" -x
uv run alembic revision --autogenerate -m "describe_change"   # after model changes, then review the file
docker build -t api:latest .                # image
```

`pytest` adds `--cov` through `addopts` (`pyproject.toml`). A unit-only run cannot reach the coverage threshold, so pass `--no-cov` for it. Use `uv run ...`; never `pip install` or activate a virtualenv by hand. If `.venv` is broken, `uv sync --reinstall`.

## Where things are *(target)*

| Need | Location |
|---|---|
| App assembly (`create_app()`), module mounting | `src/app/main.py`, `src/app/modules/__init__.py` (`MODULES`) |
| Launcher (uvicorn, dev worker and scheduler) | `run.py` |
| Public core facade | `src/app/core/__init__.py` |
| Settings and environments | `src/app/core/config.py` (`Settings`, `get_settings`, `SettingsDep`) |
| DB engine, session, `DbSession`, `Base`, `utcnow` | `src/app/core/database.py` |
| Errors: `AppError` family, RFC 9457 handlers, DB error details, `log_unexpected_error` | `src/app/core/errors.py` |
| Security: JWT encode/decode, Argon2 password hashing, token generation and hashing | `src/app/core/security.py` |
| Request middleware (request id, access log, HTTP metrics, security headers) | `src/app/core/middleware.py` |
| `/health`, `/health/ready`, `/metrics` | `src/app/core/health.py` |
| Cache (`cached`, `invalidate`, `RedisDep`) | `src/app/core/cache.py` |
| Logging setup, `get_logger` | `src/app/core/logging.py` |
| Taskiq (`task` decorator, `TaskDbSession`, `TransientTaskError`, broker, retry and scheduler builders), email (core layout `src/app/core/templates/base.html`) | `src/app/core/tasks.py`, `src/app/core/mail.py`; worker and scheduler root `src/app/worker.py` |
| Module contract (`Module`), admin (sqladmin) base | `src/app/core/module.py`, `src/app/core/admin.py` |
| Users: `User` model, profile, `CurrentUser`, `OptionalUser` | `src/app/modules/users/` |
| Auth: register, login, refresh, logout, sessions, password and email flows, audit log | `src/app/modules/auth/` |
| Migrations | `alembic/versions/`, async `alembic/env.py` |
| Tests | `tests/unit/`, `tests/integration/` (see Testing) |
| Deployment | `deployment/docker/api/` (Swarm), `deployment/docker/monitoring/` |

## Core vs modules *(target)*

- `src/app/core/` is the infrastructure. `src/app/modules/` holds the modules. Two modules are built in: `users` (the `User` entity, profile, later settings) and `auth` (how a user proves identity: credentials, sessions, tokens, audit log). See [ADR 0008](documentation/adr/0008-module-layout-and-users-auth-split.md).
- **Imports point one way: `core ← users ← auth ← app modules`.** The core never imports from `app.modules`; `users` never imports `auth`; a module never imports a module that depends on it. Modules import infrastructure from `app.core`, and `CurrentUser`, `OptionalUser`, `User` from `app.modules.users`.
- **URLs follow the resource, code follows the responsibility.** Endpoints for the signed-in user live under `/users/me/...`; flows for signed-out users live under `/auth/...`. `PUT /users/me/password` and `PUT /users/me/email` need credentials and verification, so the `auth` module serves them (OpenAPI tag `users`).
- `app.core` is a facade. It exports `Settings`, `DbSession`, `SettingsDep`, `get_settings`, `Base`, `utcnow`, `get_logger`, `log_unexpected_error`, the `AppError` family, `cached`, `invalidate`, `RedisDep`, `task` (taskiq's shared task decorator with database errors sanitized), `TaskDbSession` (a task's database session), `TransientTaskError` and `Module`. Import from `app.core`, not from its submodules.
- An app built on this boilerplate adds modules. If a task seems to need a core edit, stop and ask: either the core has a gap (a core change with an ADR) or the code belongs in a module.

## Module layout *(target)*

Every module uses the same file names, and only these. A module creates the files it needs; it never adds other names. Code that fits none of them is infrastructure (core) or a sign the module should be split. A unit test enforces the vocabulary.

```
src/app/modules/<name>/
  __init__.py       module = Module(...) and the module's public exports
  router.py         HTTP routes: thin, typed, one service call each
  schemas.py        Pydantic request/response models
  service.py        business logic; owns the transaction
  crud.py           database queries; no business rules
  models.py         SQLAlchemy models
  dependencies.py   FastAPI dependencies
  errors.py         AppError subclasses with the module's error codes
  tasks.py          taskiq tasks (`@task`)
  admin.py          sqladmin views
  templates/        email templates (never named `base.html`; that is the core layout)
```

## Adding a module *(target)*

1. Create the package `src/app/modules/<name>/` using the file names from "Module layout".
2. In `__init__.py`, define `module = Module(name=..., router=..., models=..., tasks=..., admin_views=...)`. `Module` is a frozen dataclass; `models`, `tasks` and `admin_views` are optional. The router carries its own `prefix` and `tags`. Module resources use `APIRouter(lifespan=...)`.
3. Add one line to `MODULES = (users.module, auth.module, ...)` in `src/app/modules/__init__.py`, after the modules it depends on. There is no auto-discovery.
4. Autogenerate a migration (`uv run alembic revision --autogenerate -m "add_<name>"`), review it, apply it locally only.
5. Add tests under `tests/unit/modules/<name>/` and `tests/integration/modules/<name>/`. Module-specific fixtures go into a `conftest.py` there; never edit the root `conftest.py`.

`create_app()` mounts every module router under `settings.API_PREFIX` (default `/api/v1`). The worker imports `module.tasks`, Alembic imports `module.models`, and the admin registers `module.admin_views`.

## Architecture rules

- Layering inside a module: `router → service → crud → models`. Routers never query the DB; services never build HTTP responses; crud contains no business rules. Create only the files the module needs.
- Routers are thin: validate via Pydantic, call one service function, return a typed response. Every route declares `response_model` (or a return annotation), `status_code`, `summary` and the error `responses` it can produce.
- Dependencies use `Annotated`: `db: DbSession`, `user: CurrentUser`. Never instantiate `AsyncSession` yourself. Use `Depends(..., scope="function")` only when cleanup must happen before the response is sent.
- Lifespan is the only startup/shutdown mechanism. No `on_event`, no work at import time.
- No shared base classes for crud/service; keep modules explicit and standalone.
- User hooks for modules are the `CurrentUser`/`OptionalUser` aliases only. Roles and permissions (D3), user events and per-module settings are deferred; do not build them. The 2FA stubs stay.

## Errors and responses *(target)*

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

- Ruff for lint and format, mypy for types. All code must pass both. Target rules: no `Optional[X]` (use `X | None`), no `List`/`Dict`/`Tuple` from typing, no naive `datetime.now()` (use `utcnow()` from `app.core`), no `print`, no commented-out code, no `assert` outside tests.
- Type every parameter and return value. `Any` needs a comment explaining why.
- Pydantic v2 only: `model_config = ConfigDict(...)`, `field_validator`, `model_validator`, `model_dump(mode="json")`. Request models set `extra="forbid"`. Separate `XCreate`, `XUpdate`, `XResponse`; never return ORM objects.
- Naming: `snake_case` functions and modules, `PascalCase` classes, `UPPER_SNAKE` constants, schemas suffixed `Request`/`Response`, exceptions suffixed `Error`.
- Structured log events, not messages: `logger.info("auth.login_success", user_id=str(user.id))`. Event names are `area.action`.
- Imports are absolute: `from app.core import DbSession`, `from app.modules.users import CurrentUser`. No circular imports.

## Comments and docstrings

House style, enforced by strict ruff in S3 (rule set `D` with the google convention, plus `ERA`).

- English throughout. Google-style docstrings with single backticks.
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

```python
"""Refresh-token sessions: rotation, revocation and reuse detection.

PostgreSQL is the only source of truth. Writes to `sessions` take the
user row lock first (lock order: email_tokens -> users -> sessions).
"""


async def revoke_user_sessions(
    db: AsyncSession, user_id: UUID, *, keep_family: UUID | None = None
) -> int:
    """Revoke every active session of a user.

    Args:
        db: Caller's unit of work; this function does not commit.
        user_id: Owner of the sessions.
        keep_family: Session family to keep (the caller's `sid`).

    Returns:
        Number of revoked sessions.
    """
```

## Security rules

- Passwords: `argon2-cffi` via `hash_password`/`verify_password` in `src/app/core/security.py`. Never store or log a plaintext password or token; tokens at rest are SHA-256 (`hash_token`).
- JWT via `PyJWT` with an explicit `algorithms=[...]` list (HS256, pinned in `src/app/core/security.py`). Never read the algorithm from the token.
- Refresh tokens live only in Postgres; the refresh gate is one atomic `UPDATE ... RETURNING`. Do not add a Redis mirror.
- Secrets are `SecretStr` in `Settings`; a startup guard rejects defaults outside `dev`/`unittest`. New secrets need a guard entry and a README configuration row.
- Auth-protected routes use `CurrentUser` (or `OptionalUser`); do not re-implement token parsing. Do not change `src/app/modules/auth/`, `src/app/modules/users/` or `src/app/core/security.py` internals unless the task is about auth.
- Error messages must not reveal whether an account exists (see Roadmap Phase 1 for the current gaps).
- Validate and bound every input (`max_length`, ranges, enums). Reject unknown fields.
- CORS, cookies and headers are configured centrally (`create_app()` and `src/app/core/middleware.py`); do not set them per route. The client IP comes only from the ASGI scope; uvicorn's proxy headers are enabled with trusted `FORWARDED_ALLOW_IPS`.
- Do not log request bodies, query strings, `Authorization`, cookies, emails or usernames. User context in logs is `user_id` only.
- Log unexpected exceptions with `log_unexpected_error(logger, event, exc)` or `error_type=type(exc).__name__`, never `error=str(e)`: database error text can contain user data.

## Database and migrations

- SQLAlchemy 2 style only: `Mapped[...]`, `mapped_column`, `select()`; no legacy `Query`. The driver is psycopg 3.
- Models live in `src/app/modules/<name>/models.py` and subclass `Base` from `app.core`. Alembic collects them through `MODULES`.
- Avoid lazy loads in async code: use `selectinload`/`joinedload` or `await session.refresh(obj, [...])`.
- `expire_on_commit=False` is set; still re-read objects you need after a commit if another transaction may have changed them.
- Model change → autogenerate → read the generated file, fix types and names, add indexes → `uv run alembic upgrade head` locally only. Downgrade must work. Prefer expand/contract for data migrations.
- New tables get `created_at`/`updated_at` `timestamptz`, UUID PK, explicit indexes for every FK and filter column.

## Caching and background jobs *(target)*

- The core offers `@cached(ttl, key)` and `invalidate(pattern)`: a thin cashews wrapper that caches return values on the one Redis client. See [ADR 0003](documentation/adr/0003-caching-via-cached-decorator.md).
- Cache keys include every input that changes the result, `{user_id}` included for per-user data. A missing component leaks data across users.
- Mutations call `invalidate(...)` in the service. TTLs come from `Settings`.
- There is no ETag middleware, no client cache-control middleware and no route-cache settings.
- All background work is a taskiq task in the module's `tasks.py`, declared with `@task(task_name=...)` from `app.core`, exported through `Module(tasks=...)` and enqueued with `await my_task.kiq(...)`. Importing a module must read no settings: the broker is built by `app.worker` (worker, scheduler) and by the API lifespan. Email templates live in the module and are passed as `send_templated_email(..., template_dir=...)`, which returns False when mail is not configured. Do not use FastAPI `BackgroundTasks`.
- Tasks receive IDs and primitives, never ORM objects, and never personal data or secrets: task arguments are stored in Redis (the stream, retry schedules) and logged by taskiq at DEBUG. Pass the id of the row the task works on (the auth token mail and the email-change notices get the email token's id, the lock notice the user's id) and let the task read the rest. Enqueue after the commit, so the worker finds the rows.
- The core `task` decorator wraps the task function: a database error (`sqlalchemy.exc.DBAPIError` or a raw `psycopg.Error`) leaves the task as `TaskDatabaseError`, whose message holds only the exception types and the SQLSTATE, because taskiq logs a failed task's exception and stores it as the result. Lost connections, deadlocks, serialization failures and admin shutdowns become `TransientTaskDatabaseError` and are retried for tasks with `retry_on_error=True`. Other exceptions pass through unchanged. Do not catch database errors in a task to log their text.
- A task gets a database session with `db: TaskDbSession = TaskiqDepends()` (`TaskiqDepends` from `taskiq`; the default only lets `kiq()` type-check without the session). One session per run; the task's service owns the transaction. Worker processes open the engine at `WORKER_STARTUP` with the API's settings (`DB_POOL_SIZE` per process) and close it at `WORKER_SHUTDOWN`; the API and the scheduler open none. See [ADR 0010](documentation/adr/0010-token-emails-rekeyed-in-the-worker.md).
- Delivery is **at least once** (Redis stream with acknowledgements, [ADR 0009](documentation/adr/0009-taskiq-redis-stream-broker-and-delayed-retries.md)): a task can run twice, so every task is idempotent.
- Retries: a task with the label `retry_on_error=True` that raises `TransientTaskError` (from `app.core`) runs again after `TASKIQ_RETRY_DELAY_SECONDS`, at most `TASKIQ_RETRY_COUNT` times. Raise it only for errors that may pass (network, timeouts, a busy server); everything else fails at once. Its message must hold no personal data, because taskiq logs it. Mail tasks raise `mail_delivery_error(exc) from None`, which picks the transient or the permanent mail error.
- Delayed retries are schedules that the scheduler sends: a worker without a running scheduler never retries. `run.py dev` starts both.
- Periodic tasks use the label `schedule=[{"cron": "0 3 * * *"}]`. taskiq 0.13 matches cron fields against `datetime.now(tz=timezone.utc)`, so the times are UTC on every host; do not set `cron_offset`. Exactly one scheduler runs, but a periodic task must still be idempotent (redelivery). Batch large deletes (one transaction per batch, `FOR UPDATE SKIP LOCKED`, no user lock), as `auth.cleanup` does.
- The retry and Prometheus middlewares belong to the worker (`app.worker`) only. The API's broker (`build_broker`) only enqueues; building the worker broker in an API process would put the API's `/metrics` into prometheus_client's multiprocess mode.

## Testing *(target)*

- `tests/unit/` needs no Docker and runs in seconds. Rule: if the code under test needs no DB or Redis, it is a unit test.
- `tests/integration/` uses `TestClient` with Postgres and Redis Testcontainers. Do not mock the DB. Patch only external side effects: the `captured_emails` fixture replaces the mailer and records every sent mail (recipient, kind, token, link, HTML).
- Tasks run in-process in integration tests: the `app` fixture's broker is taskiq's `InMemoryBroker` with the worker's database hooks, so `kiq()` runs the task before the request answers, against the test database. A task that fails fails the test (`client` fixture). `run_task(client, task, *args)` runs a task outside a request. Tests of the Redis transport (stream entries, retry schedules) use `redis_broker_client`, where nothing consumes the stream.
- Layout: `tests/unit/{core,modules/<name>}` and `tests/integration/{core,contract,modules/<name>,admin}`. A new module brings its own `conftest.py` and never edits the root one.
- There is no backend e2e folder. End-to-end journeys are covered by frontend Playwright.
- Do not test third-party libraries. Guard our own configuration of them.
- Each new endpoint gets: happy path, validation error, auth failure (missing, invalid, expired token), not-found or conflict, and one test proving the side effect (audit row, email, cache invalidation).
- Tests are independent. The autouse `clean_db` fixture truncates the application tables **before** each test. Use plain helper functions (`register`, `login`, `bearer`) as in the existing auth tests.
- Every test must fail without the change it covers. Keep the suite deterministic (no sleeps, no real network).
- The coverage threshold is enforced by pytest-cov on the full run; do not lower it to make a PR pass.
- ADR: [0007](documentation/adr/0007-test-split-unit-integration.md).

## Definition of done

- [ ] `uvx ruff@0.15.0 check src tests` and `format --check` pass
- [ ] `uv run mypy src` passes
- [ ] `uv run pytest` passes locally with Docker running
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

## Specialised agents

Claude Code project agents live in `../.claude/agents/` (repository root) and are listed in [CLAUDE.md](CLAUDE.md).
