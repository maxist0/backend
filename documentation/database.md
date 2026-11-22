# Database

How the backend talks to PostgreSQL: engine and sessions, transactions, query rules and migrations. Form: explanation with reference tables. The tables are described in [Schema](#schema-users-and-auth) below; the rules for code are in [AGENTS.md → Database and migrations](../AGENTS.md#database-and-migrations).

## Stack

| Part | Choice |
|---|---|
| Database | PostgreSQL 18 (`compose.dev.yaml`, the test containers). The Swarm stack still runs 16 (Roadmap Phase 6) |
| ORM | SQLAlchemy 2.1, async, `Mapped[...]` and `mapped_column`, `select()`; no legacy `Query` |
| Driver | psycopg 3 (`postgresql+psycopg`), the only driver. A `postgresql+asyncpg` or `+psycopg2` URL is refused |
| Migrations | Alembic with an async `env.py` |

The versions are in `uv.lock`.

## Configuration

`DATABASE_URL` and the pool settings are in [README → Configuration](../README.md#configuration). `postgresql://` and `postgres://` are rewritten to `postgresql+psycopg://`; any other driver stops startup with a message that names the scheme, never the URL.

Pool values are **per process**. Every API process and every taskiq worker process opens its own pool of `DB_POOL_SIZE` plus up to `DB_MAX_OVERFLOW` connections, so PostgreSQL's `max_connections` must cover all of them. Up to `(API processes + worker processes) × (DB_POOL_SIZE + DB_MAX_OVERFLOW)` connections can be open at once.

## Engine and sessions

`src/app/core/database.py`:

- `build_engine(settings)` creates the async engine: `pool_pre_ping=True` (stale connections are discarded), `hide_parameters=True` (bound values stay out of SQL error messages), `connect_timeout` from `DB_CONNECT_TIMEOUT`, no echo. It logs the URL with the password masked.
- `create_session_factory()` returns an `async_sessionmaker` with `expire_on_commit=False` and `autoflush=False`. `create_app()` creates it unbound; the lifespan binds it to the engine after a `SELECT 1` check and unbinds it on shutdown.
- `DbSession` is `Annotated[AsyncSession, Depends(get_db)]`. Use it in routes and dependencies; never create an `AsyncSession` yourself. `get_db` rolls the session back when the request raises.
- `Base` is the declarative base of every model; `utcnow()` returns a timezone-aware UTC `datetime` for timestamp columns.
- Nothing is created at import time. The engine and the pool exist only inside the lifespan (API) and in a worker's startup hook.

On Windows psycopg needs a `SelectorEventLoop`; the lifespan fails fast with a clear message on the default `ProactorEventLoop` ([README → Quick start](../README.md#quick-start)).

## Transactions

The service owns the transaction: it calls `commit()` once its work is done, and enqueues background tasks only after that. A crud function does not commit in a new module ([modules.md](modules.md#step-5-the-queries)). The `users` and `auth` crud functions still commit themselves; moving that into the services is Roadmap Phase 4.

`expire_on_commit=False` means objects keep their loaded values after a commit, without a hidden reload. Re-read an object after a commit when another transaction may have changed it.

## Queries

- Use `select()` with explicit columns or entities, and `await db.execute(...)`.
- Avoid lazy loads in async code (they raise): use `selectinload` or `joinedload` in the query, or `await db.refresh(obj, [...])`.
- Return ORM objects from crud and service functions and convert them to response models in the service or router; never return an ORM object from a route.
- Bound every list: a route takes `limit` and `offset` with a maximum; order by a stable key.
- Lock rows in the documented order: `email_tokens` → `users` → `sessions` ([authentication.md](authentication.md#serialisation)).
- Database error text can contain user data. Code that reacts to a database error uses `constraint_name(exc)` or the SQLSTATE from `app.core`, and logs with `log_unexpected_error`.

## Schema: users and auth

The five tables of the built-in `users` and `auth` modules. Source of truth: the models (`src/app/modules/users/models.py`, `src/app/modules/auth/models.py`) and the Alembic migrations. This section was checked on 2026-11-22 against `\d <table>` of a database migrated to the head revision. Application defaults (`uuid4()`, `utcnow()`, `False`, `0`) are set by SQLAlchemy, so the columns have no server defaults.

All primary keys are UUIDv4 generated in the application (open decision D10 covers UUIDv7). Timestamps are `timestamptz` in UTC. Your own modules follow the same rules: see [modules.md](modules.md).

```
users 1 ── 1 password_credentials
users 1 ── n sessions
users 1 ── n email_tokens
users 1 ── n auth_audit_log     (user_id is set to NULL when the user is deleted)
```

### `users` (module `users`)

The identity of an account.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | primary key |
| `username` | varchar(64) | no | unique (`users_username_key`); letters, digits, `-` and `_` (checked by the request schema) |
| `email` | varchar(255) | no | unique (`users_email_key`); stored lower case |
| `display_name` | varchar(128) | yes | |
| `is_active` | boolean | no | `false` blocks login, refresh and access tokens |
| `is_email_verified` | boolean | no | |
| `is_locked` | boolean | no | with `locked_until` NULL: admin lock; with a time: timed lock |
| `failed_login_attempts` | integer | no | counter for the timed lock |
| `locked_until` | timestamptz | yes | end of a timed lock |
| `created_at`, `updated_at` | timestamptz | no | |
| `last_login_at` | timestamptz | yes | |

Lock semantics are in [authentication.md](authentication.md#two-kinds-of-lock).

### `password_credentials` (module `auth`)

One row per user.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | primary key |
| `user_id` | uuid | no | unique, foreign key to `users.id` `ON DELETE CASCADE` |
| `password_hash` | varchar(512) | no | Argon2id hash |
| `must_change` | boolean | no | set by the admin action "Force password change"; not enforced yet (known issue 16) |
| `created_at`, `updated_at` | timestamptz | no | |

### `sessions` (module `auth`)

One row per refresh token. A rotated token stays as a row so reuse can be detected.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | primary key |
| `user_id` | uuid | no | foreign key `ON DELETE CASCADE`; index `ix_sessions_user_id` |
| `token_hash` | varchar(64) | no | SHA-256 of the refresh token; unique (`sessions_token_hash_key`) |
| `family_id` | uuid | no | all rotations of one login share it; index `ix_sessions_family_id` |
| `is_revoked` | boolean | no | |
| `revoke_reason` | varchar(64) | yes | for example `rotated`, `logout_all`, `password_reset` |
| `expires_at` | timestamptz | no | index `ix_sessions_expires_at` |
| `device_id`, `device_name` | varchar(255) | yes | from the `X-Device-*` headers, cut to 255 characters |
| `ip_address` | varchar(45) | yes | |
| `user_agent` | varchar(512) | yes | cut to 512 characters |
| `created_at` | timestamptz | no | |
| `revoked_at` | timestamptz | yes | |

The nightly `auth.cleanup` task deletes ended sessions `AUTH_CLEANUP_RETENTION_DAYS` after their latest timestamp.

### `email_tokens` (module `auth`)

Single-use tokens mailed to a user. Only the SHA-256 hash of the token value is stored.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | primary key; this id, not the token, goes into the task |
| `user_id` | uuid | no | foreign key `ON DELETE CASCADE`; index `ix_email_tokens_user_purpose` (`user_id`, `purpose`) |
| `purpose` | `emailtokenpurpose` | no | `VERIFY_EMAIL`, `CHANGE_EMAIL`, `RESET_PASSWORD` |
| `token_hash` | varchar(64) | no | index `ix_email_tokens_token_hash` |
| `new_email` | varchar(255) | yes | the requested address of a `CHANGE_EMAIL` token |
| `previous_email` | varchar(255) | yes | the replaced address, set when a change is confirmed, so the worker can notify it |
| `expires_at` | timestamptz | no | index `ix_email_tokens_expires_at` |
| `used_at` | timestamptz | yes | set when the token is redeemed |
| `invalidated_at` | timestamptz | yes | set when a newer token or a security event closes it |
| `created_at` | timestamptz | no | |

The cleanup task deletes closed tokens, and with them `new_email` and `previous_email`, after the retention.

### `auth_audit_log` (module `auth`)

Append-only record of security-relevant events.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | uuid | no | primary key |
| `user_id` | uuid | yes | foreign key `ON DELETE SET NULL`; index `ix_audit_log_user_id` |
| `event_type` | `auditeventtype` | no | index `ix_audit_log_event_type` |
| `ip_address` | varchar(45) | yes | |
| `user_agent` | varchar(512) | yes | |
| `detail` | text | yes | short context, for example "Password confirmation" or `... by admin <name>` |
| `created_at` | timestamptz | no | index `ix_audit_log_created_at` |

PostgreSQL stores the enum member names, in upper case: `REGISTER`, `LOGIN_SUCCESS`, `LOGIN_FAILED`, `ACCOUNT_LOCKED`, `ACCOUNT_UNLOCKED`, `EMAIL_VERIFICATION_SENT`, `EMAIL_VERIFIED`, `EMAIL_CHANGED`, `PASSWORD_CHANGED`, `PASSWORD_RESET_REQUESTED`, `PASSWORD_RESET_COMPLETED`, `SESSION_REVOKED`, `ACCOUNT_DEACTIVATED`, `ACCOUNT_REACTIVATED`, `PASSWORD_CHANGE_FORCED`, `ACCOUNT_LOCKED_NOTICE_SENT`. The cleanup task does not touch this table.

### Revisions

| Revision | Message |
|---|---|
| `37a0d49f8bbc` | add auth and user tables |
| `8b82d1b725c0` | add admin audit event types |
| `14e82733e84f` | add `previous_email` and the lock-notice audit event |

Applied migrations are never edited (see [Migrations](#migrations)).

## Migrations

Alembic's `env.py` imports `app.modules`, so `Base.metadata` holds every table of every registered module. It reads `DATABASE_URL` and builds no application settings, so a migration job needs no application secrets. With `ENVIRONMENT=dev` a missing `DATABASE_URL` is filled from `.env.local`; injected variables always win. Without `ENVIRONMENT=dev` the file is never read.

| Task | Command |
|---|---|
| Apply all migrations | `uv run alembic upgrade head` |
| Create a revision after a model change | `uv run alembic revision --autogenerate -m "add_x"` |
| Roll back one revision | `uv run alembic downgrade -1` |
| Show the current revision | `uv run alembic current` |
| Compare models and database | `uv run alembic check` |

Each command needs `DATABASE_URL` in the environment, or `ENVIRONMENT=dev` and a `.env.local` that defines it.

- File names follow `alembic.ini`: `YYYYMMDD_HHMM_<revision>_<slug>.py`, timestamps in UTC.
- After writing a revision, the post-write hooks in `alembic.ini` run ruff `--fix` and `ruff format` on it. Read the file anyway: autogenerate is a draft.
- Never edit an applied migration. The downgrade must work; the example in [modules.md](modules.md#step-11-the-migration) shows the full sequence.
- Prefer expand and contract for data migrations. New tables get a UUID primary key, `created_at` and `updated_at` as `timestamptz` and an index for every foreign key and filter column.
- Apply migrations to your local database only. The Swarm stack has a one-shot `migrate` service that runs `alembic upgrade head`; the application never migrates at startup.
- The first migration was written with an older template; it is the one file with ignored ruff rules in `pyproject.toml`.

## Inspecting the dev database

```bash
docker compose -f compose.dev.yaml exec postgres psql -U postgres -d api -c '\dt'
docker compose -f compose.dev.yaml exec postgres psql -U postgres -d api -c '\d users'
```

These are the public development credentials of `compose.dev.yaml`. There is no backup tooling in this repository; backups belong to the deployment.

## Related

- [modules.md](modules.md): a model and its migration, step by step.
