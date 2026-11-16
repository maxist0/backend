# AGENTS.md — backend

Guidance for coding agents (Claude Code, Codex, Cursor, Copilot) and humans working in this repository. Human-facing docs, architecture and the roadmap are in [README.md](README.md); do not duplicate them here, link to them.

## Project in one paragraph

Production-grade FastAPI boilerplate: async SQLAlchemy 2 on PostgreSQL, Redis for the route cache, taskiq broker and results (refresh tokens live only in Postgres), taskiq for background jobs, structlog + Prometheus for observability, Docker Swarm for deployment. Python **3.14** is the target. The repository intentionally contains **no business features**; keep it generic. Current gaps and the plan are in [README.md → Roadmap](README.md#roadmap) and [Known issues](README.md#known-issues). Decisions not yet taken are in [Open decisions](README.md#open-decisions); do not pre-empt them in code.

## Golden rules

1. Async only. Never call sync DB, Redis, SMTP or HTTP from request handlers or tasks.
2. Read before you write: `src/common/config.py`, `src/common/exceptions.py`, `src/common/responses.py`, and the feature folder you touch.
3. Every value that could differ per environment comes from `Settings`. No hard-coded URLs, TTLs, limits, origins or secrets.
4. Never commit secrets, `.env.local`, or real credentials. `.env` is a template with empty values.
5. Generate migrations, never apply them outside your local database. Never edit an applied migration.
6. No new dependency without stating why and checking it is maintained (see "Do not use").
7. Tests are part of the change. A feature without tests is not done.

## Setup and commands

```bash
uv python install 3.14 && uv sync          # install (creates .venv with dev group)
uv run alembic upgrade head                 # migrate local DB (reads .env.local)
uv run python app.py dev                    # API on :8000 + taskiq worker, reads .env.local
uvx ruff@0.15.0 check src tests             # lint   (strict config is Roadmap Phase 0)
uvx ruff@0.15.0 format src tests            # format
uv run mypy src                             # types  (strict mode is Roadmap Phase 0)
uv run pytest                               # all tests; needs a running Docker daemon (Postgres and Redis Testcontainers)
uv run pytest tests/auth/test_auth.py::TestLogin -x
uv run pytest -k "refresh" -x -p no:cacheprovider
uv run alembic revision --autogenerate -m "describe_change"   # after model changes, then review the file
docker build -t api:latest .                # image
```

Use `uv run ...`; never `pip install` or activate a virtualenv by hand. If `.venv` is broken, `uv sync --reinstall`.

## Where things are

| Need | Location |
|---|---|
| App assembly, middleware order, routers | `src/main.py` |
| Settings and environments | `src/common/config.py` (`Settings`, `Environment`) |
| DB engine, session, `get_db` | `src/common/database.py`; typed alias `DbSession` in `src/common/dependencies.py` |
| Auth dependencies (`get_current_user`, `get_optional_user`) | `src/api/auth/dependencies.py` |
| JWT encode/decode | `src/utils/jwt_utils.py` |
| Response envelope helpers | `src/common/responses.py`, models in `src/common/schemas.py` |
| Global exception handlers | `src/common/exceptions.py` |
| Logging | `src/common/logging.py` (`get_logger(__name__)`, `log_unexpected_error`) |
| DB error details (constraint, SQLSTATE) | `src/common/db_errors.py` (`constraint_name`, `sqlstate`, `find_db_error`) |
| Metrics | `src/common/metrics.py` (`DatabaseTimer`, counters) |
| Cache (Redis + cashews) | `src/common/cache.py` |
| Background tasks | `src/common/taskiq.py` (broker), tasks in `src/utils/*.py` |
| Email | `src/common/mail.py`, templates in `src/templates/email/` |
| Migrations | `alembic/versions/`, async `alembic/env.py` |
| Tests and fixtures | `tests/conftest.py` (`client`, `clean_db`, `captured_emails`) |
| Deployment | `deployment/docker/api/` (Swarm), `deployment/docker/monitoring/` |

## Architecture rules

- One feature = one folder `src/api/<feature>/` with `router.py`, `service.py`, `crud.py`, `models.py`, `schemas.py`, `dependencies.py`, `exceptions.py`, `__init__.py`. Create only the files the feature needs; an empty file is worse than a missing one. Mirror the folder in `tests/<feature>/`.
- Layering is strict: `router → service → crud → models`. Routers never query the DB; services never build HTTP responses; crud contains no business rules.
- Routers are thin: validate via Pydantic, call one service function, return a typed response. Every route declares `response_model` (or a return annotation), `status_code`, `summary` and the error `responses` it can produce.
- Dependencies use `Annotated`: `db: DbSession`, `current_user: Annotated[User, Depends(get_current_user)]`. Never instantiate `AsyncSession` yourself. Use `Depends(..., scope="function")` only when cleanup must happen before the response is sent.
- Register routers in `src/main.py` with `prefix` and `tags`. API versioning (`/api/v1`) is the next roadmap step; new routers should be written so the prefix can move without code changes.
- Lifespan (`src/main.py`) is the only startup/shutdown mechanism. No `on_event`, no work at import time.
- Shared base classes for crud/service are not used; keep modules explicit and standalone.

## Code style

- Ruff for lint and format, mypy for types. All code must pass both. The strict Ruff rule set from the roadmap is the target even before it is enabled: no `Optional[X]` (use `X | None`), no `List`/`Dict` from typing, no naive `datetime.now()` (use `datetime.now(timezone.utc)`), no `print`, no commented-out code, no `assert` outside tests.
- Type every parameter and return value. `Any` needs a comment explaining why.
- Pydantic v2 only: `model_config = ConfigDict(...)`, `field_validator`, `model_validator`, `model_dump(mode="json")`. Request models should set `extra="forbid"`. Separate `XCreate`, `XUpdate`, `XResponse`; never return ORM objects.
- Naming: `snake_case` functions and modules, `PascalCase` classes, `UPPER_SNAKE` constants, schemas suffixed `Request`/`Response`, exceptions suffixed `Error`.
- Docstrings on every route, service and crud function; route docstrings are user-facing (they render in Swagger).
- Structured log events, not messages: `logger.info("auth.login_success", user_id=str(user.id))`. Event names are `area.action`.
- Imports: absolute (`from src.api.auth import crud`). No circular imports between features; shared code goes to `src/common/`.

## API conventions

- REST nouns, plural, kebab-case paths; actions as sub-resources (`/auth/password/reset`), not verbs in the resource name.
- Status codes: 200 read/update, 201 create, 204 delete, 400 bad input, 401 missing or invalid credentials, 403 authenticated but not allowed, 404, 409 conflict, 422 validation, 429 rate limited, 500 never with internals.
- Error bodies use the envelope in `src/common/responses.py` until decision D1 (RFC 9457) is taken. Raise named exceptions from services; do not return `(None, "message")` tuples in new code and do not wrap handlers in `except Exception`.
- Reads must be idempotent and cacheable; mutations must invalidate the relevant cashews keys in the service.
- Timestamps RFC 3339 in UTC; IDs are UUID strings.
- Never expose stack traces, SQL, or internal identifiers in responses.

## Security rules

- Passwords: `argon2-cffi` via `hash_password`/`verify_password` in `src/api/auth/service.py`. Never store or log a plaintext password or token; tokens at rest are SHA-256 (`hash_token`).
- JWT via `PyJWT` with an explicit `algorithms=[...]` list (HS256, pinned in `src/utils/jwt_utils.py`). Never read the algorithm from the token.
- Refresh tokens live only in Postgres; the refresh gate is one atomic `UPDATE ... RETURNING`. Do not add a Redis mirror.
- Secrets are `SecretStr` in `Settings`; a startup guard rejects defaults outside `dev`/`unittest`. New secrets need a guard entry and a README configuration row.
- Auth-protected routes use `get_current_user`; do not re-implement token parsing. Do not change `src/api/auth/` internals unless the task is about auth.
- Error messages must not reveal whether an account exists (see Roadmap Phase 1 for the current gaps).
- Validate and bound every input (`max_length`, ranges, enums). Reject unknown fields.
- CORS, cookies and headers are configured centrally in `src/main.py`; do not set them per route.
- Do not log request bodies, query strings, `Authorization`, cookies, emails or usernames. User context in logs is `user_id` only.
- Log unexpected exceptions with `log_unexpected_error(logger, event, exc)` or `error_type=type(exc).__name__`, never `error=str(e)`: database error text can contain user data.

## Database and migrations

- SQLAlchemy 2 style only: `Mapped[...]`, `mapped_column`, `select()`; no legacy `Query`.
- Avoid lazy loads in async code: use `selectinload`/`joinedload` or `await session.refresh(obj, [...])`.
- `expire_on_commit=False` is set; still re-read objects you need after a commit if another transaction may have changed them.
- Model change → `uv run alembic revision --autogenerate -m "..."` → read the generated file, fix types and names, add indexes → `uv run alembic upgrade head` locally only. Downgrade must work. Prefer expand/contract for data migrations.
- New tables get `created_at`/`updated_at` `timestamptz`, UUID PK, explicit indexes for every FK and filter column.

## Caching and background jobs

- Route cache: `@cache(ttl=cache_control_ttl(default=settings.ROUTE_CACHE_TTL), key="route:<name>")` from `cashews` on idempotent GETs only. TTLs come from `Settings`.
- Background work that may be lost is fine in FastAPI `BackgroundTasks`; everything else is a taskiq task in `src/utils/`, registered in `src/common/taskiq.py`, enqueued with `await task.kiq(...)`, and must be retry-safe (idempotent).
- Tasks receive IDs and primitives, never ORM objects.

## Testing

- Integration tests through `TestClient` against real Postgres and Redis (Testcontainers). Do not mock the DB. Patch only external side effects (email dispatch via the `captured_emails` fixture).
- Each new endpoint gets: happy path, validation error, auth failure (missing, invalid, expired token), not-found or conflict, and one test proving the side effect (audit row, email, cache invalidation).
- Tests are independent; `clean_db` truncates auth tables after every test. Use plain helper functions (`register`, `login`, `bearer`) as in `tests/auth/test_auth.py`.
- Every test must fail without the change it covers. Keep the suite deterministic (no sleeps, no real network).
- Coverage threshold is enforced by pytest-cov; do not lower it to make a PR pass.

## Definition of done

- [ ] `uvx ruff@0.15.0 check src tests` and `format --check` pass
- [ ] `uv run mypy src` passes
- [ ] `uv run pytest` passes locally with Docker running
- [ ] New settings added to `src/common/config.py`, `.env` template and the README configuration table
- [ ] Migration generated and reviewed if models changed; command listed in the PR description
- [ ] README sections touched by the change are updated in the same PR (layout, design, roadmap checkbox)
- [ ] No TODOs without an issue reference; no debug output; no new `# type: ignore` without a reason

## Git and pull requests

- Conventional Commits: `feat(auth): rotate refresh token family on reuse`, `fix(cache): ...`, `chore(deps): ...`, `docs: ...`.
- One logical change per commit; keep `uv.lock` churn out of unrelated commits.
- PR description: what, why, how to verify, migration and config notes, roadmap item reference.
- Agents do not commit, push, tag or open PRs unless explicitly asked in the task.

## Boundaries

**Always:** run lint, types and tests before reporting done; keep changes minimal and local; update docs with code; ask when a decision in README → Open decisions is needed.

**Ask first:** adding or upgrading a dependency; changing `pyproject.toml` tooling config, `Dockerfile`, anything under `deployment/`; changing middleware order, security headers, CORS or cookie settings; cross-feature refactors; deleting files; touching `alembic/env.py`.

**Never:** apply migrations against a non-local database; run `docker stack deploy`, `docker stack rm` or anything against a Swarm manager; commit secrets or `.env.local`; disable or weaken a security check, a lint rule or a test to make something pass; log credentials, tokens or PII; use `python-jose`, `passlib`, `fastapi-cache2`, `arq`, or sync DB drivers; add business-domain features to this boilerplate.

## Do not use

`python-jose` (unpatched critical CVE), `passlib` (unmaintained, breaks on bcrypt ≥ 4.1), `fastapi-cache2` (dormant), `arq` (maintenance only), Celery for async code, `requests` in async paths (use `httpx`), `datetime.utcnow()`, `ORJSONResponse` (deprecated in FastAPI 0.131, declare a `response_model` instead).

## Specialised agents

Claude Code project agents live in `../.claude/agents/` (repository root) and are listed in [CLAUDE.md](CLAUDE.md). Use `backend-engineer` for feature work, `db-engineer` for schema and migrations, `backend-tester` for tests, `security-auditor` for reviews, `observability-engineer` for logging and metrics, `deploy-engineer` for Docker and Swarm, `docs-writer` for documentation, `researcher` for web research.
