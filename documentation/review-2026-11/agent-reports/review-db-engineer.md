# Data-layer review (db-engineer), 2026-11-16

Scope: `src/common/{database,models,dependencies}.py`, `src/api/auth/{models,crud,service}.py`, `src/api/user/{models,crud,service}.py`, `alembic/env.py`, `alembic/versions/20260226_2120_37a0d49f8bbc_add_auth_user_tables.py`, `tests/conftest.py` (DB setup only), plus `src/admin.py` and `src/api/auth/router.py` where they change DB behaviour.

Mode: read-only. No repository file was changed, no migration was generated or applied.

## 1. Outcome

The data layer has two critical defects: refresh-token family revocation does not work, and Alembic autogenerate sees an empty metadata. Seven high-severity problems follow, mostly lost updates and partial commits caused by the commit-per-crud design, plus two places that log the database password. I recommend one breaking **new migration baseline** that adds the model base, naming convention, UUIDv7, constraints and indexes. Before it ships, two hotfixes that need no schema change: DB-01 and DB-09.

Severity counts: **critical 2, high 7, medium 15, low 6** (30 findings).

## 2. Changes

None. The only file written is this report.

## 3. Verification performed (evidence)

| Check | Command / source | Result |
|---|---|---|
| Installed versions | `uv.lock` | SQLAlchemy 2.0.49, Alembic 1.18.4, asyncpg 0.31.0, psycopg 3.3.3, FastAPI 0.136.1, sqladmin 0.25.0, redis 7.4.0, taskiq 0.12.2; `requires-python = ">=3.13"` |
| What `env.py` gives Alembic as metadata | `.venv/Scripts/python.exe -I -c "from src.common.models import Base; print(sorted(Base.metadata.tables))"` | `[]`, so the metadata is empty |
| Whether `env.py` can simply import the models | same process, then `import src.api.auth.models` | `AttributeError: 'NoneType' object has no attribute 'DATABASE_URL'`, raised in `database._build_engine` at import |
| Whether `op.drop_table` drops PostgreSQL enum types | read `alembic/ddl/impl.py:461`, `alembic/operations/ops.py:1380-1397` (DropTableOp.to_table without `_reverse` builds a Table with no columns) and `sqlalchemy/dialects/postgresql/named_types.py:115` | It does not. No `Enum` column means no `DROP TYPE` |
| Session `autocommit` parameter | `sqlalchemy/orm/session.py:1512` | `autocommit: Literal[False] = False`, a no-op kept for compatibility |
| `.env.local` defines `DATABASE_URL` | `grep -c '^DATABASE_URL' .env.local` (count only, no value printed) | 1 |

**Not run:** migrations, pytest, `EXPLAIN (ANALYZE, BUFFERS)`. The brief and agent boundaries forbid applying migrations, and the review was read-only. All plan or locking statements below are reasoned from PostgreSQL semantics, not measured.

Primary sources consulted, all on 2026-11-16:
- PyPI JSON for SQLAlchemy: latest 2.1.8, `requires_python >=3.11`.
- SQLAlchemy 2.1 migration notes: https://docs.sqlalchemy.org/en/21/changelog/migration_21.html. Relevant points:
  - greenlet only via `[asyncio]`;
  - unconditional autoflush;
  - default PostgreSQL driver is now psycopg;
  - Enum is tied to MetaData, and `Table.drop()` no longer drops types;
  - `uuidv7()` is supported as a monotonic sentinel (`monotonic=True`) in batched INSERT.
- SQLAlchemy 2.1 changelog via secondary mirrors: 2.1.5 has asyncio cancellation fixes (#13673, #13548) and 2.0.55 backports many of them. **Unverified against the official page.**
- PostgreSQL 18.7 docs §9.14 (https://www.postgresql.org/docs/current/functions-uuid.html): `uuidv7([shift interval])` generates a "version 7 (time-ordered) UUID ... UNIX timestamp with millisecond precision + sub-millisecond timestamp + random".
- PostgreSQL 18.7 docs §8.12 (UUID type).
- RFC 9562:
  - §2.1: "UUID versions that are not time ordered, such as UUIDv4 ..., have poor database-index locality."
  - §5.7: v7 is preferred over v1/v6.
  - §6.12: treat UUIDs as opaque.
  - §8: "Timestamps embedded in the UUID do pose a very small attack surface"; UUIDs "MUST NOT be used as security capabilities".
- Python 3.14 `uuid` docs: `uuid.uuid7()`, "Added in version 3.14", 42-bit counter for monotonicity within a millisecond.
- FastAPI docs, Dependencies with yield: with the default scope `"request"`, "the exit code of dependencies with yield is executed after the response is sent". `scope="function"` exits before the response. A request-scoped dependency cannot have function-scoped sub-dependencies.
- Alembic autogenerate docs and maintainer statements: changes to PostgreSQL enum values are not detected; use `op.execute("ALTER TYPE ... ADD VALUE")` or the third-party `alembic-postgresql-enum`.

## 4. Known-issue verification

| Known issue | Status | Evidence |
|---|---|---|
| 2: conftest runs `upgrade head` and `create_all` on the same DB and shares the engine across loops | **Confirmed, and worse than described** (see DB-08) | `tests/conftest.py:62-74`. `alembic/env.py:36-39` reloads `.env.local` with `override=True`, so `upgrade head` runs against the **developer's local database**. The container gets its schema from `create_all` only, so the migration is never exercised in tests on a developer machine. |
| 11: `env.py` loads `.env.local` with `override=True` | **Confirmed**; consequence added in DB-08 | `alembic/env.py:38-39` |
| Roadmap Phase 4, unit of work | **Confirmed** | 11 crud functions commit (`crud.py:69,77,91,114,125,148,170,198,225,245,256`) |
| Roadmap Phase 4, naming convention | **Confirmed missing** | `src/common/models.py:13-16` has no `MetaData` |
| Roadmap Phase 4, session cleanup job | **Confirmed missing** | `src/utils/tasks.py` contains only a heartbeat cron |
| Roadmap Phase 4, SQLAlchemy 2.1 | **Still open**; 2.1.8 is GA | DB-24 |
| README "Session store: Postgres as source of truth" | **Wrong** | DB-10: Postgres is never used to accept a valid token |
| README "All primary keys are UUIDv4 generated in the application" | Correct | `models.py:53-55` and others. `family_id` also uses v4 (`service.py:329`) |

---

## 5. Findings

Format: ID, severity, category, then the location, evidence, impact, fix, effort, whether it breaks anything, and the README reference.

### DB-01: Refresh fast path ignores DB revocation, so family revocation is ineffective. Critical, bug (security logic).

- **Where:** `src/api/auth/service.py:370-392`; `src/api/auth/crud.py:151-171`.
- **Evidence:**
  - When Redis has the token, the code fetches the DB session and calls `revoke_session`. It never checks `sess.is_revoked` or `sess.expires_at`, then issues a new session in the same family (`create_session_tokens(..., family_id=family_id)`).
  - `revoke_family` only updates Postgres. Redis keys are the raw tokens (`crud.py:39-40`), so the server cannot delete the family's live Redis entries.
- **Impact:** This is the textbook stolen-token case.
  1. The attacker uses stolen T1 and receives T2, which is now cached.
  2. The victim later presents T1. Reuse is detected and the family is revoked in the DB.
  3. T2 is still in Redis, so the attacker keeps rotating indefinitely (T2 → T3 → …).
  4. The rows the attacker creates are fresh and unrevoked, so the "revoked" family keeps living.
  5. The same hole will defeat the planned "revoke all sessions on password reset" and `logout-all` (Phase 1, Known issue 5).
- **Fix:**
  - **Hotfix, no schema change:** in the fast path, treat `sess is None or sess.is_revoked or sess.expires_at <= now` exactly like the reuse branch.
  - **Proper fix (DB-03):** the DB conditional `UPDATE … RETURNING` becomes the only arbiter, and Redis is removed from the refresh path or reduced to a cache keyed by hash.
- **Effort:** S (hotfix), M (proper fix).
- **Breaking:** no.
- **README:** NEW (related to Known issue 5).

### DB-02: Alembic sees an empty `MetaData`; autogenerate would drop every table and `alembic check` fails. Critical, bug.

- **Where:** `alembic/env.py:27,61`; `src/common/database.py:21,63`; `src/api/auth/models.py:27`.
- **Evidence:**
  - `env.py` imports only `src.common.models.Base`, which imports no models. A verified run gives `Base.metadata.tables == []`.
  - Adding `import src.api.auth.models` to `env.py` is not enough. The models import `Base` from `src.common.database`, which builds the engine at import time from the `settings` global. That global is `None` until `init_settings()` runs (`config.py:156`), and `env.py` never calls it. The verified run raises `AttributeError`.
- **Impact:**
  - The documented workflow (`AGENTS.md`: `uv run alembic revision --autogenerate`) produces `drop_table` for all five tables and their indexes. One careless review means total data loss.
  - The roadmap item "`alembic check` in CI" cannot pass. It would report `remove_table` operations.
- **Fix:**
  1. Models import `Base` from `src.common.models` (no I/O).
  2. Add a model registry `src/common/model_registry.py` that imports every feature's `models` module. `env.py` and the tests import it.
  3. Build the engine lazily (`get_engine()` or created in the lifespan) instead of at import time. This also satisfies the AGENTS.md rule "no work at import time".
  4. Reasoned out: once these import fixes are in, `alembic check` against the current migration passes. Types, lengths, nullability, unique constraints, FK `ondelete` and index names all match. Enum value lists are not compared.
- **Effort:** S.
- **Breaking:** no (touches `env.py`, so "ask first").
- **README:** NEW.

### DB-03: Concurrent refresh with the same token creates parallel valid chains, and reuse goes undetected. High, bug (race).

- **Where:** `service.py:371-392`; `crud.py:139-148`.
- **Evidence:** `GET` (`:371`) and `DELETE` (`:374`) are separate round trips, and the result of `delete_cached_refresh_token` is ignored. `revoke_session` is an unconditional `UPDATE … WHERE id = :id`. Two requests that both pass the GET each create a new session in the family.
- **Impact:** A replayed token raced against the legitimate client yields two live chains with no reuse signal. Benign multi-tab races also fork the family, which makes the later reuse detection fire on legitimate users.
- **Fix:** make Postgres the arbiter with one atomic statement:

  ```sql
  UPDATE sessions
     SET revoked_at = now(), revoke_reason = 'rotated'
   WHERE token_hash = :h AND revoked_at IS NULL AND expires_at > now()
  RETURNING id, user_id, family_id;
  ```

  - No row returned: look up `token_hash`. If it exists, it is reuse, so revoke the family with one `UPDATE … RETURNING`. Otherwise the token is invalid.
  - Insert the new session in the same transaction.
  - Under READ COMMITTED a second concurrent `UPDATE` blocks on the row lock, re-evaluates the `WHERE` after the first commits, and matches 0 rows. No `FOR UPDATE` and no raised isolation level are needed.
  - If Redis stays, use `GETDEL` (Redis ≥ 6.2) and key by hash.
  - **Optional design improvement:** one row per device session with `token_hash` + `previous_token_hash` + `generation`, instead of one row per rotation. This removes about 96 rows per active client per day (DB-23) and makes "list my sessions" trivial. Reuse is detected when the presented hash matches `previous_token_hash`.
- **Effort:** M.
- **Breaking:** behaviour only (schema unchanged, or changed in the baseline).
- **README:** NEW.

### DB-04: Failed-login counter is a read-modify-write, so the lockout can be bypassed with parallel requests. High, bug (race).

- **Where:** `service.py:215, 267-297`; `constants.py:5-6`.
- **Evidence:**
  - `user.failed_login_attempts += 1` runs on a row read earlier, then an ORM `UPDATE … SET failed_login_attempts = <python value>` writes it back.
  - Argon2 verification is roughly 50 ms or more of synchronous CPU, which widens the window. Three replicas multiply it.
  - N parallel wrong guesses that all read 0 all write 1.
  - In addition, the counter never decays: `LOGIN_ATTEMPT_WINDOW_SECONDS` is used as the lock duration, not as a window. Five typos spread over months lock the account.
- **Impact:** The lockout security control is defeated by concurrency.
- **Fix:** one atomic statement:

  ```sql
  UPDATE users
     SET failed_login_attempts = failed_login_attempts + 1,
         locked_until = CASE WHEN failed_login_attempts + 1 >= :max
                             THEN now() + :lock ELSE locked_until END
   WHERE id = :id
  RETURNING failed_login_attempts, locked_until;
  ```

  Add `last_failed_login_at` and reset the counter when it is older than the window. Long-term, move throttling to Redis with `limits` (Phase 1, D4) and keep the DB columns for audit.
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW (complements Phase 1 rate limiting).

### DB-05: An admin "Lock" is undone at the next login attempt. High, logic.

- **Where:** `src/admin.py:87-95`; `service.py:221-243`.
- **Evidence:** The admin action sets `is_locked=True` and leaves `locked_until` NULL. The login treats `is_locked AND (locked_until IS NULL OR locked_until <= now)` as "lock expired", unlocks the account and continues.
- **Impact:** The support or security control "lock user" silently does nothing.
- **Root cause:** one boolean carries two meanings, a temporary brute-force throttle and an administrative suspension.
- **Fix (data model):**
  - Drop `is_locked`.
  - Use `locked_until timestamptz NULL` for the throttle (locked if `locked_until > now()`).
  - Use `is_active = false` or `suspended_at timestamptz` for administrative suspension.
  - Add `CHECK (failed_login_attempts >= 0)`.
- **Effort:** S.
- **Breaking:** yes (column removed; done in the baseline).
- **README:** NEW.

### DB-06: No unit of work; every crud call commits, which causes partial writes. High, logic.

- **Where:** `crud.py:69,77,91,114,125,148,170,198,225,245,256`; services at `service.py:145-197, 481-506, 514-540`, `user/service.py:58-101`.
- **Evidence and impact:**
  - **Registration** runs four or five commits. If any later step fails (example: DB-07's long `User-Agent` makes the audit insert fail), the user, credential and token are committed, the response is 500, no email is sent, and the username is burned.
  - **`reset_password`** marks the token used (commit), then updates the hash (commit), then audits (commit). If the audit fails, the password is changed but the client gets 500.
  - **`update_profile`**: `display_name` is committed as a side effect of `invalidate_tokens_for_user_purpose`'s commit (`user/service.py:74,86`) even if a later step fails.
  - **`authenticate_user`** commits `last_login_at`, then the audit, then `create_session_tokens` commits again. Lock/unlock and audit are not atomic.
- **Fix:**
  - crud functions only `add`, `flush` or `execute`.
  - The service closes the use case with one commit, through the helper in §7.
  - Side effects (`kiq`) run **after** the commit. Otherwise a worker may read rows that do not exist yet.
  - For guaranteed delivery, a transactional outbox table can come later.
- **Effort:** M.
- **Breaking:** internal API only.
- **README:** Roadmap Phase 4 item 1, **confirmed**.

### DB-07: Attacker-controlled header lengths overflow bounded `varchar` columns, causing a 500 after partial commits. High, bug.

- **Where:** `auth/models.py:160-161, 251-254`; `service.py:133-134, 337-340`.
- **Evidence:**
  - `user_agent varchar(512)`, `device_id`/`device_name varchar(255)` and `ip_address varchar(45)` are filled straight from `User-Agent`, `X-Device-Id`, `X-Device-Name` and `request.client.host` with no truncation.
  - PostgreSQL raises `value too long for type character varying(n)` on overflow.
  - The router's `except Exception` turns it into a 500 (`router.py:99`).
- **Impact:**
  - A client with a long User-Agent can never log in. `update_user` has already committed, and then `_audit` fails.
  - Registrations end half-committed (DB-06).
  - Any anonymous caller can trigger it.
- **Fix:**
  - Store `user_agent` as `text`, truncated in the service to a bound kept in `Settings`.
  - Validate and bound the `X-Device-*` headers.
  - Store `ip_address` as `inet`, which validates and is smaller.
  - The DB length limit should mirror a business rule, not truncate input by surprise.
- **Effort:** S.
- **Breaking:** schema types (baseline).
- **README:** NEW.

### DB-08: Test harness migrates the developer's database, tests a different driver, and uses a different PostgreSQL major. High, bug (tooling).

- **Where:** `tests/conftest.py:29-37, 62-74`; `alembic/env.py:36-54`.
- **Evidence:**
  1. `conftest` sets `DATABASE_URL` to the container, but `command.upgrade` loads `env.py`, which runs `load_dotenv(".env.local", override=True)`. `.env.local` defines `DATABASE_URL` (verified by count). The migration therefore runs against the developer's local database. The container schema then comes only from `create_all`, so the migration under test is never applied to the test database on a developer machine. It is in CI, where `.env.local` is absent, so CI and local runs diverge.
  2. `env.py` also ignores the `sqlalchemy.url` that conftest sets through `set_main_option`, because it overwrites it from `os.environ`.
  3. Tests use `postgresql+psycopg://` (psycopg 3). Production uses `asyncpg`. Driver-specific behaviour is untested: prepared-statement cache, error classes, codecs, cancellation.
  4. Tests use `postgres:15-alpine`, Swarm uses 16 and the README uses 18.
- **Impact:** Tests silently mutate a developer database. Migrations are effectively unverified locally. Production driver paths are untested.
- **Fix:**
  - `env.py` must never load dotenv with override; prefer the `-x` arguments or the already-set `sqlalchemy.url`.
  - Run tests on `postgresql+asyncpg` against `postgres:18-alpine` (once the target major is confirmed).
  - Build the schema **only** through `alembic upgrade head`, never `create_all`.
  - Add a migration round-trip test: `upgrade head` → `downgrade base` → `upgrade head`, which would have caught DB-11.
  - Add an `alembic check` test.
  - Create the engine per event loop (Known issue 2).
- **Effort:** M.
- **Breaking:** no.
- **README:** Known issues 2 and 11 **confirmed**, consequence NEW.

### DB-09: The database password is written to logs. High, bug (security).

- **Where:** `src/common/database.py:88` (`logger.info("db.pool_init", url=settings.DATABASE_URL)`); `alembic/env.py:49` (`print(f"[alembic] Using DATABASE_URL: {_url}")`).
- **Evidence:** Line 41 masks the password with `render_as_string(hide_password=True)`. Line 88 logs the raw URL, and `env.py` prints it on every migration run, including the Swarm `migrate` one-shot service.
- **Impact:** Credentials end up in stdout, Loki and CI logs. That is OWASP A09 and contradicts AGENTS.md "never log secrets".
- **Fix:** Log `make_url(url).render_as_string(hide_password=True)` or only host and database; remove the `print`s.
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW (fits Phase 3 log redaction).

### DB-10: Postgres is not actually the source of truth for refresh tokens. Medium, logic.

- **Where:** `service.py:394-416, 385`; `crud.py:39-40, 275`.
- **Evidence:**
  - When Redis misses, a valid, unrevoked, unexpired DB session is rejected ("Invalid or expired refresh token").
  - Raw refresh tokens are Redis keys (`rt:<raw>`), which contradicts "tokens at rest are SHA-256" (AGENTS.md security rules).
  - Refresh does not check `user.is_active` or the lock state.
- **Impact:**
  - A Redis restart or eviction logs out every user.
  - Raw bearer secrets sit in Redis memory, RDB and AOF files.
  - Deactivated users keep obtaining access tokens. They are rejected later by `get_current_user`, but they are still minted.
- **Fix:** Covered by DB-03: the DB is the arbiter, with an optional Redis cache keyed by `sha256`. Check `is_active` and `locked_until` inside the same statement, with `UPDATE … FROM users`.
- **Effort:** M (with DB-03).
- **Breaking:** no.
- **README:** Design-decisions row "Session store" is **wrong**; NEW.

### DB-11: Downgrade leaves the enum types behind, so `upgrade` → `downgrade` → `upgrade` fails. Medium, bug.

- **Where:** migration `downgrade()` lines 149-163.
- **Evidence:**
  - `op.drop_table("…")` builds a Table without columns (`DropTableOp.to_table`, no `_reverse`), so `DROP TYPE auditeventtype` and `DROP TYPE emailtokenpurpose` are never emitted.
  - On re-upgrade, `create_table` fires `before_create` with `checkfirst=False` (`alembic/ddl/impl.py:439-440`), giving `DuplicateObject`.
  - SQLAlchemy 2.1 formalises this: "`Table.drop()` does not drop them".
- **Impact:** The mandatory verification step in the agent workflow cannot pass, and downgrade is not usable.
- **Fix:** Moot if DB-12 removes native enums. Otherwise call `sa.Enum(name=...).drop(op.get_bind(), checkfirst=True)` in `downgrade()`.
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW.

### DB-12: Native PostgreSQL enums for value sets that evolve. Medium, best-practice and extensibility.

- **Where:** `auth/models.py:131-143, 157-159, 180-183, 197-199`; migration lines 45-63, 82-91.
- **Evidence:**
  - Autogenerate does not detect added enum values (Alembic docs and maintainer).
  - Adding an `AuditEventType` member (Phase 1 adds logout-all, invitation and email-changed flows) produces no migration. The first insert then fails with `invalid input value for enum`.
  - Values cannot be dropped.
  - SQLAlchemy persists enum **names** (`'LOGIN_SUCCESS'`), not the Python values (`'login_success'`), which is surprising for raw SQL and analytics.
- **Fix:** `text` + named `CHECK (event_type IN (...))` constraint, or `text` validated only in the app for the open-ended audit event set. `CHECK` changes are visible in diffs; add them with `NOT VALID` + `VALIDATE`. Use `values_callable` or store values. Keep native enums only for sets that are truly closed.
- **Trade-off:** 1–2 bytes larger per row than a 4-byte enum OID, which is negligible.
- **Effort:** S (baseline).
- **Breaking:** yes (baseline).
- **README:** NEW.

### DB-13: Check-then-insert uniqueness races, with no `IntegrityError` mapping. Medium, bug (race).

- **Where:** `service.py:158-170`; `user/service.py:79-83`; `router.py:99`.
- **Evidence:** `SELECT` by username/email, then `INSERT`. Two concurrent registrations both pass the check, and one hits `UniqueViolation`, which the router turns into a 500.
- **Impact:** Wrong status code (500 instead of 409), noisy error logs and partial state (DB-06).
- **Fix:**
  - Insert, catch `IntegrityError`, and map `e.orig.sqlstate == '23505'` plus the **constraint name** (deterministic once DB-17 is in) to a domain `UsernameTakenError` / `EmailTakenError`.
  - Keep the pre-check only for a friendly fast path.
  - Note that the email-change pre-check is an enumeration oracle (Phase 1 topic).
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW (Phase 2 named exceptions).

### DB-14: Case-insensitive uniqueness is not enforced by the database. Medium, best-practice and security.

- **Where:** `auth/models.py:56-57`; `schemas.py:24-29`.
- **Evidence:**
  - Email is lowercased only in Pydantic (`RegisterRequest`, `UpdateProfileRequest`). The admin panel, raw SQL and future import paths bypass it.
  - `username` is unique case-sensitively, so `Admin` and `admin` can coexist, which enables impersonation.
- **Fix:**
  - `UNIQUE INDEX uq_users_username_lower ON users (lower(username))`.
  - `CHECK (email = lower(email))` plus a plain unique, or `citext` (needs the extension).
  - Log in by `lower(username) = lower(:u)` so the expression index is used.
- **Effort:** S.
- **Breaking:** yes (baseline; a decision on username case semantics is needed, see questions).
- **README:** NEW.

### DB-15: Email-token single use is not atomic, and indexes and constraints are missing. Medium, bug.

- **Where:** `crud.py:202-245`; `service.py:491-501, 523-537`; `auth/models.py:200, 222-226`.
- **Evidence:**
  - `get_valid_email_token` (SELECT) is followed by `mark_email_token_used` (an unconditional UPDATE). Concurrent requests both succeed.
  - `token_hash` has a non-unique index.
  - Nothing enforces "one active token per user and purpose". Two concurrent `forgot` calls each invalidate and then insert, leaving two live tokens.
  - `CHANGE_EMAIL` rows may have a NULL `new_email`.
- **Fix:**
  - Consume with one statement: `UPDATE email_tokens SET used_at = now() WHERE token_hash = :h AND purpose = :p AND used_at IS NULL AND invalidated_at IS NULL AND expires_at > now() RETURNING user_id, new_email`.
  - `UNIQUE (token_hash)`.
  - Partial unique index `(user_id, purpose) WHERE used_at IS NULL AND invalidated_at IS NULL`.
  - `CHECK (purpose <> 'change_email' OR new_email IS NOT NULL)`.
  - `CHECK (num_nonnulls(used_at, invalidated_at) <= 1)`.
- **Effort:** S.
- **Breaking:** yes (baseline).
- **README:** NEW.

### DB-16: No server defaults, application clock used, `updated_at` maintained by hand. Medium, best-practice.

- **Where:** every `created_at`/`updated_at` and boolean column (`auth/models.py:60-77, 111-118, 164-166, 213-215, 244, 256-258`); `crud.py:75, 112`.
- **Evidence:**
  - Defaults are Python-only (`default=utcnow`, `default=False`). The migration has no `server_default`.
  - Rows inserted by SQL (admin console, seed, data migration) fail `NOT NULL` or get the wrong time.
  - Timestamps come from three replicas' clocks.
  - `update_user` sets `updated_at` manually in addition to `onupdate`.
- **Fix:**
  - `server_default=func.now()`, `onupdate=func.now()`, and `server_default=text('false')` for booleans.
  - **Must be combined with `eager_defaults=True`.** With `expire_on_commit=False`, server-generated values are otherwise expired after flush, and reading them in async code raises `MissingGreenlet`. The 2.0 default `"auto"` covers INSERT RETURNING only, not UPDATE.
  - See the mixins in §7.
- **Effort:** S.
- **Breaking:** baseline.
- **README:** Roadmap Phase 4 item 2 **confirmed**.

### DB-17: No naming convention. Medium, best-practice.

- **Where:** `src/common/models.py:13-16`; migration lines 37-39, 68, 98, 121-123, 140-142.
- **Evidence:** PK, UQ and FK names are generated by PostgreSQL (`users_email_key`, `sessions_token_hash_key`, `email_tokens_user_id_fkey` and so on), while indexes are hand-named. Names are not reproducible from metadata, so later `drop_constraint` calls need guessing. `IntegrityError` cannot be mapped by name (DB-13).
- **Fix:** `MetaData(naming_convention=…)` in `Base` (§7). Introducing it renames constraints, which is free in a new baseline and costly as an incremental migration.
- **Effort:** S.
- **Breaking:** baseline.
- **README:** Roadmap Phase 4 item 5 **confirmed**.

### DB-18: Redundant state and missing `CHECK` constraints. Medium, best-practice.

- **Where:** `sessions.is_revoked` + `revoked_at` (`:244, 259`); `users.is_locked` + `locked_until`; `users.failed_login_attempts`; `email_tokens`, `sessions` `expires_at`.
- **Evidence:** Two sources of truth per state. Nothing prevents `is_revoked = false` together with a `revoked_at`, negative counters, or `expires_at <= created_at`.
- **Fix:**
  - Derive booleans from timestamps: drop `is_revoked` and use `revoked_at IS NULL`. Optionally replace `is_email_verified` with `email_verified_at`.
  - Add `CHECK (failed_login_attempts >= 0)`, `CHECK (expires_at > created_at)`, `CHECK (revoke_reason IS NULL OR revoked_at IS NOT NULL)`, `CHECK (length(token_hash) = 64)` or switch to `bytea` with `octet_length = 32`.
- **Effort:** S.
- **Breaking:** baseline.
- **README:** NEW.

### DB-19: Index set does not match the queries. Medium, performance.

- **Where:** `auth/models.py:173-177, 222-226, 269-272`.

| Index | Query it serves | Verdict |
|---|---|---|
| `ix_audit_log_event_type` | none in code; 12 distinct values, low selectivity | **drop**. Every audit insert pays for it. |
| `ix_audit_log_user_id` | FK `SET NULL` on user delete | replace with `(user_id, created_at DESC)`. It serves both the FK and a future "my security events" keyset listing. |
| `ix_audit_log_created_at` (btree) | retention delete | keep. At more than about 10⁷ rows consider BRIN (append-only, time-correlated) or partitioning (DB-23). |
| `ix_email_tokens_token_hash` (non-unique) | token lookup | make it **unique** (DB-15) |
| `ix_email_tokens_user_purpose` | invalidate-previous | make it a **partial unique** index `WHERE used_at IS NULL AND invalidated_at IS NULL`: smaller, and it enforces the invariant |
| `ix_email_tokens_expires_at` | nothing yet (no cleanup) | keep for the cleanup job (DB-23) |
| `ix_sessions_user_id` | FK cascade; `get_active_sessions_for_user` (unused) | keep; add `WHERE revoked_at IS NULL` as a second partial index when "list my sessions" ships |
| `ix_sessions_family_id` | family revoke | make it partial `WHERE revoked_at IS NULL` (revoked rows dominate after rotation) |
| missing `sessions (expires_at)` | cleanup | add |

- **Write cost:** each dropped index removes one B-tree insert and its WAL on the hottest audit table. Partial indexes shrink with revoked and used rows. No index touches `users.updated_at` or `last_login_at`, so the per-login `users` update stays HOT-eligible; keep it that way.
- **Measurement:** none. Validate with `EXPLAIN (ANALYZE, BUFFERS)` on seeded data (for example 1M sessions, 5M audit rows).
- **Effort:** S.
- **Breaking:** baseline.
- **README:** NEW.

### DB-20: Engine and session configuration. Medium, best-practice and modernisation.

- **Where:** `database.py:35-75`; `config.py:26-29`.
- **Evidence:**
  - The engine is built at import time from a mutable global (DB-02).
  - No server-side guards: `statement_timeout`, `idle_in_transaction_session_timeout`, `lock_timeout` are all unset.
  - No `application_name` (for `pg_stat_activity` and the Grafana Postgres dashboard).
  - No asyncpg `command_timeout`.
  - `future=True` (deprecated no-op in 2.0) and `autocommit=False` (no-op) are noise.
  - `pool_pre_ping=True` and `pool_recycle=1800` are fine.
  - JIT: OLTP lookups here never reach `jit_above_cost`, so this is optional.
  - PgBouncer is not deployed. If it is added in transaction mode, `prepared_statement_cache_size=0` plus `prepared_statement_name_func` are required.
- **Fix:** sketch, values from `Settings`:

  ```python
  create_async_engine(
      url,
      pool_size=..., max_overflow=..., pool_timeout=..., pool_recycle=..., pool_pre_ping=True,
      connect_args={
          "command_timeout": settings.DB_COMMAND_TIMEOUT,
          "server_settings": {
              "application_name": settings.APP_NAME,
              "statement_timeout": str(settings.DB_STATEMENT_TIMEOUT_MS),
              "idle_in_transaction_session_timeout": str(settings.DB_IDLE_TX_TIMEOUT_MS),
              "lock_timeout": str(settings.DB_LOCK_TIMEOUT_MS),
          },
      },
  )
  ```

  Create it lazily in the lifespan, or through `get_engine()`, and dispose it in `close_db`.
- **Effort:** S.
- **Breaking:** no (changes `database.py`, so "ask first").
- **README:** NEW (relates to the Phase 3 settings item).

### DB-21: `get_db` lifecycle against FastAPI's request scope. Medium, logic.

- **Where:** `database.py:105-113`; `dependencies.py:22`; all routers (`router.py:99` and others).
- **Evidence:**
  - In FastAPI 0.136 a yield dependency with the default `"request"` scope exits **after the response is sent** (FastAPI docs).
  - Routers wrap everything in `except Exception` and return an error response, so `get_db`'s `rollback` branch never runs.
  - `get_current_user` already ran a `SELECT`, so the session is auto-begun and holds a connection idle-in-transaction until after the response, including during Argon2 hashing and Redis calls.
- **Impact on the unit-of-work design:**
  - Committing in `get_db`'s exit would commit **after** the client received 2xx, and it would commit partial state when the router swallowed an exception.
  - `scope="function"` cannot be used on `get_db` while request-scoped dependencies (`get_current_user`) depend on it. That is the FastAPI sub-dependency rule.
  - `async with db.begin()` in services raises "a transaction is already begun", because of autobegin from the auth dependency.
- **Fix:** Services commit explicitly through the `transaction(db)` helper (§7). `get_db` keeps rollback/close only. Routers stop catching `Exception` (Phase 2). Set `idle_in_transaction_session_timeout` (DB-20).
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW (prerequisite for Roadmap Phase 4 item 1).

### DB-22: `env.py` is not safe for production migrations. Medium, best-practice.

- **Where:** `alembic/env.py:36-54, 88-109, 124`.
- **Evidence:**
  - `override=True` (Known issue 11).
  - `assert` for configuration, which is stripped under `-O`.
  - `print` statements, including the secret (DB-09).
  - No `lock_timeout`/`statement_timeout` inside migrations. A migration waiting for an `ACCESS EXCLUSIVE` lock blocks all traffic behind it during a start-first rolling update.
  - No `transaction_per_migration=True`.
  - No `process_revision_directives` to suppress empty autogenerate revisions.
  - The offline mode duplicates the configuration.
- **Fix:**
  - Run `SET lock_timeout = '5s'; SET statement_timeout = '15min'` on the migration connection, overridable per revision.
  - Raise `RuntimeError` on a missing URL.
  - Use the `alembic` logger, not `print`.
  - `transaction_per_migration=True`.
  - Use a `process_revision_directives` hook that drops empty autogenerated revisions.
  - Add a review checklist to `script.py.mako`: expand/contract step, `CONCURRENTLY` via `autocommit_block`, `NOT VALID` and lock behaviour.
- **Effort:** S.
- **Breaking:** no ("ask first": `env.py`).
- **README:** Known issue 11 **confirmed**; rest NEW (Roadmap Phase 4 item 5).

### DB-23: No cleanup, retention or erasure path; tables grow without bound. Medium, performance and privacy.

- **Where:** `src/utils/tasks.py` (heartbeat only); `auth/models.py:146-177, 229-272`.
- **Evidence and estimates (assumptions):**
  - **Sessions:** one row per rotation. An access token lasts 15 minutes, so a continuously active client writes about 96 rows per day. With a 30-day lifetime, 10k active clients reach roughly 29M rows a month, and nothing deletes them.
  - **Audit:** one row per auth event, including failed logins of known users, with no retention.
  - **Email tokens:** keep `new_email` (personal data) forever.
  - **GDPR:** on user deletion the audit rows survive with `ip_address` and `user_agent` (`SET NULL`), which is still personal data.
- **Fix:**
  - A taskiq cron with batched deletes (`DELETE … WHERE ctid IN (SELECT ctid … LIMIT 5000)` in a loop, each batch committed):
    - sessions with `expires_at < now() - grace`;
    - email tokens past expiry, plus a grace period;
    - audit rows older than `AUDIT_RETENTION_DAYS` (new setting).
  - Tasks open their own session through `AsyncSessionFactory.begin()` (a helper in §7). Workers currently have no DB lifecycle.
  - **Erasure:** anonymise `ip_address`/`user_agent` of the user's audit rows in the same transaction as the user delete. A trigger or service step is acceptable.
  - **Partitioning** `auth_audit_log` by month: only when volume passes about 50M rows or retention must drop whole months instantly. Not for the boilerplate default; document it as the scale path.
  - **Append-only:** `REVOKE UPDATE, DELETE ON auth_audit_log FROM app_role`. The retention job then needs a separate role or a `SECURITY DEFINER` function.
- **Effort:** M.
- **Breaking:** no.
- **README:** Roadmap Phase 4 item 4 **confirmed**; audit/GDPR part NEW.

### DB-24: SQLAlchemy 2.1 upgrade and Python floor. Medium, modernisation.

- **Where:** `pyproject.toml:5, 18`.
- **Evidence:**
  - 2.1.8 is GA and requires Python ≥ 3.11.
  - The project already uses `sqlalchemy[asyncio]` (greenlet is pulled correctly).
  - Relevant 2.1 changes:
    - default PostgreSQL driver is psycopg, so URLs must keep `+asyncpg` (they do);
    - autoflush is unconditional for all statements when enabled (this project sets `autoflush=False`; inferred to have no impact, verify in tests);
    - Enum types are bound to MetaData and `Table.drop()` no longer drops them;
    - `uuidv7()` is usable as a monotonic `server_default` sentinel in batched INSERT (`monotonic=True`);
    - asyncio cancellation fixes in 2.1.5 (#13673; from a secondary source, verify).
  - `requires-python = ">=3.13"` contradicts the decision "≥ 3.14". `uuid.uuid7()` (D10) needs 3.14.
- **Fix:**
  - `sqlalchemy[asyncio]>=2.1.8,<2.2` and `requires-python = ">=3.14"`.
  - Remove `future=True` and `autocommit=False`.
  - Do this **after** DB-08, so the suite runs on asyncpg + PG 18 and actually exercises the change.
- **Effort:** S.
- **Breaking:** dependency floor.
- **README:** Roadmap Phase 4 item 6 **confirmed open**.

### DB-25: Relationship loading defaults are unsafe for async. Low, best-practice.

- **Where:** `auth/models.py:86-95, 123, 171, 220, 267`.
- **Evidence:**
  - Default `lazy="select"`. Any attribute access outside an awaited load raises `MissingGreenlet`.
  - `cascade="all, delete-orphan"` without `passive_deletes=True` makes an ORM delete (for example sqladmin "Delete user") load **every** session and token row before deleting them.
  - `audit_logs` without `passive_deletes` loads and nulls every audit row through the ORM.
- **Fix:** `lazy="raise"` on all relationships, `passive_deletes=True` (the DB `ON DELETE` already does the work), and drop `User.audit_logs` or make it `viewonly`. No N+1 exists today because the relationships are unused outside admin.
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW.

### DB-26: Pool sizing against `max_connections`. Low, performance.

- **Where:** `config.py:26-27`; `deployment/docker/api/docker-compose.yml:99, 220, 260, 280`.
- **Evidence:**
  - API: (10 + 20) × 3 replicas, plus 1 during start-first, gives 120.
  - taskiq workers would add (10 + 20) × 4 processes × 2 replicas = 240 once tasks use the database (the cleanup job will).
  - That exceeds `max_connections = 200`, before admin, migrate and exporter connections.
- **Fix:**
  - Separate pool settings for workers (`DB_POOL_SIZE = 2`, `DB_MAX_OVERFLOW = 0`).
  - Document `(pool_size + max_overflow) × processes × replicas ≤ max_connections − reserve`.
  - Consider PgBouncer later (then see DB-20 for asyncpg).
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW. Deployment is out of scope; only the settings are flagged.

### DB-27: `password_credentials` surrogate key, and `must_change` is not enforced. Low, best-practice and logic.

- **Where:** `auth/models.py:98-123`; `admin.py:127-131`.
- **Evidence:**
  - This is a 1:1 table with its own UUID PK plus `UNIQUE(user_id)`, so two indexes where one suffices.
  - `must_change` is set by the admin action "Force password change" but read nowhere in `src/`. The control does nothing.
- **Fix:** Use `user_id` as the PK. Enforce `must_change` at login (Phase 1 auth task) or remove the column and the admin action.
- **Effort:** S.
- **Breaking:** baseline.
- **README:** NEW.

### DB-28: Query hygiene. Low, best-practice.

- **Where:** `crud.py:51, 57, 63, 101, 136, 151-171, 174-187, 183, 213`; `user/models.py`, `user/crud.py`.
- **Evidence:**
  - `.scalars().first()` on unique lookups; `scalar_one_or_none()` would assert uniqueness.
  - Python `utcnow()` in `WHERE` (app clock) instead of `func.now()`.
  - `revoke_family` does a `SELECT` and then an `UPDATE` with a race between them; one `UPDATE … RETURNING` is enough.
  - `get_active_sessions_for_user` is dead code.
  - Every query is wrapped by `DatabaseTimer`. Fine for now, but redundant once OTel SQLAlchemy instrumentation arrives (Phase 3).
  - The `user/` re-export shims are covered in Roadmap Phase 0.
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW (shims are a Phase 0 item).

### DB-29: Type modernisation. Low, modernisation.

- **Where:** `auth/models.py`.
- **Proposals:**
  - `ip_address` → `inet`.
  - `token_hash` → `bytea` (32 bytes, half the size of 64 hex characters; index entries shrink accordingly).
  - `is_email_verified` → `email_verified_at timestamptz`.
  - `revoke_reason` → `text` + `CHECK` against a closed set.
  - `password_hash varchar(512)` → `text` (Argon2 PHC strings are about 100 characters; the limit is not a business rule).
- **Effort:** S.
- **Breaking:** baseline.
- **README:** NEW.

### DB-30: Documentation drift. Low, docs.

- **Where:**
  - `README.md:257` (UUIDv4) and `:294` ("Postgres as source of truth", wrong per DB-10).
  - `documentation/database.md` (describes `autocommit=False` as "explicit transaction control" and an `ix_users_email` index that does not exist).
  - `documentation/database_schemas/` is empty.
- **Fix:** Update with the baseline (docs-writer). Add an ER diagram and per-table lifecycle (retention, erasure) to `database_schemas/`.
- **Effort:** S.
- **Breaking:** no.
- **README:** NEW.

---

## 6. Open decision D10: primary key strategy

**Recommendation:** UUIDv7 for all primary keys (and `family_id`). Generate them in the application with Python 3.14's stdlib `uuid.uuid7()` as `default=`. Add `server_default=text("uuidv7()")` only once PostgreSQL 18 is the confirmed floor. Keep UUIDv4 as a per-table opt-out for resources whose creation time must not be exposed. Reject bigint as the default.

Reasoning:

| Criterion | UUIDv4 (today) | UUIDv7 | bigint identity |
|---|---|---|---|
| B-tree insert locality | random leaf, page splits across the whole index, more full-page-write WAL after checkpoints. RFC 9562 §2.1: "poor database-index locality" | right-most leaf, like a sequence (time-ordered, ms plus counter) | best |
| Size | 16 B | 16 B | 8 B (saving immaterial at boilerplate scale) |
| Safe to expose / enumeration (OWASP API1 BOLA aid) | yes | yes for guessing: 74 random bits; Python's counter consumes some randomness, still infeasible to guess. RFC §8: never a capability | **no**, sequential and enumerable; needs a second public id |
| Leaks | nothing | creation time to the millisecond (RFC §8: "very small attack surface"). For `users.id` this exposes the signup time through the JWT `sub` and URLs. Acceptable for a generic boilerplate; opt-out per table | row count and growth rate |
| ID known before INSERT (build graphs, idempotency, outbox) | yes | yes (app-side) | no (needs RETURNING or a sequence round trip) |
| Works on PG 15/16 (tests and Swarm today) | yes | yes (app-side); `uuidv7()` server default needs PG 18 | yes |
| Batched INSERT (`insertmanyvalues`) | fine | app-side fine; server-side needs SQLAlchemy 2.1 `monotonic=True` | fine |
| Natural sort by creation (keyset pagination, Phase 2) | no, needs `(created_at, id)` | `ORDER BY id` approximates creation order (ms precision, monotonic within one process per Python docs, not across processes) | yes |

Notes:
- Generating in the application means the clock comes from the replicas. Ordering across replicas is only approximately monotonic, which is good enough for index locality. Do not rely on `id` order for correctness; use `created_at` for business ordering.
- Migration path for existing data: not needed with the new baseline. For downstream projects already deployed, v7 can be introduced on new rows only, because the column type stays `uuid`.
- The frontend already uses UUIDv7 for client-generated keys (approved 2026-11-15), so the convention is consistent across packages.

---

## 7. Proposed model base, transaction and repository pattern

### 7.1 Model base (`src/common/models.py`). A sketch, to be verified against SQLAlchemy 2.1 docs at implementation time.

```python
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, MetaData, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_N_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True), uuid.UUID: UUID(as_uuid=True)}

    @declared_attr.directive
    def __mapper_args__(cls) -> dict[str, Any]:
        args: dict[str, Any] = {"eager_defaults": True}   # fetch server defaults via RETURNING (async-safe)
        if "version" in cls.__dict__ or any("version" in b.__dict__ for b in cls.__mro__):
            args["version_id_col"] = cls.__table__.c.version  # verify attribute access in a mixin
        return args

class UUIDPrimaryKey:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid7, sort_order=-100)

class Timestamps:
    created_at: Mapped[datetime] = mapped_column(server_default=func.now(), sort_order=900)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now(), sort_order=901)

class Versioned:  # opt-in: user-editable rows; pairs with ETag/If-Match (Phase 2)
    version: Mapped[int] = mapped_column(server_default=text("1"))

class SoftDelete:  # opt-in only; never for users (GDPR erasure = hard delete/anonymise)
    deleted_at: Mapped[datetime | None]
    # every unique on such a table must become a partial unique index WHERE deleted_at IS NULL
```

Decisions:
- **Naming convention:** deterministic names, diffable migrations, `IntegrityError` mapping by name. `CheckConstraint` must always carry `name=`.
- **`eager_defaults=True`:** mandatory with `server_default` / `onupdate=func.now()` and `expire_on_commit=False` in async (DB-16).
- **Optimistic locking:** opt-in, because it only pays off where two writers can edit the same row (profile, future domain entities). Bulk `update()` statements bypass it and must bump `version` explicitly (the admin actions do bulk updates).
- **Soft delete:** opt-in and documented, not on the base. It complicates every unique constraint and query.
- **`updated_at` trigger:** none. `onupdate` covers ORM and Core `update()`; raw SQL writers set it themselves. A trigger adds hidden behaviour, the usual trade-off.

### 7.2 Transaction / unit of work. Challenging AGENTS.md "no shared base classes for crud/service".

My position: **keep the rule for repositories and services, but clarify it.** Generic `BaseRepository[T]` classes hide queries, encourage N+1 and `get_all()` without bounds, and add nothing over explicit functions for a boilerplate this size. However, two shared, non-inheritance primitives are needed for correctness. They are not base classes, so the rule stands, but its wording should say so explicitly:

1. **Model mixins** (7.1). Without them every feature re-implements PK, timestamp and naming rules and drifts.
2. **Transaction helpers** in `src/common/database.py`:

```python
@asynccontextmanager
async def transaction(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Commit on success, roll back on any error. Works with autobegun sessions (get_current_user already queried)."""
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise

@asynccontextmanager
async def task_session() -> AsyncIterator[AsyncSession]:
    """Session for taskiq tasks / cron (no FastAPI dependency available)."""
    async with get_sessionmaker().begin() as session:
        yield session
```

Rules:
- crud: no `commit()`. Use `flush()` when the caller needs generated values or wants constraint errors surfaced early.
- service: exactly one `async with transaction(db):` per use case. Enqueue tasks **after** the block. A transactional outbox comes later if delivery must be guaranteed.
- `get_db`: yield, rollback on error, close. It never commits, because its exit runs after the response (DB-21).
- Proposed AGENTS.md wording: "No generic repository or service base classes. Shared model mixins (`src/common/models.py`) and transaction helpers (`src/common/database.py`) are mandatory."

---

## 8. New migration baseline: justification

Recommended: replace `37a0d49f8bbc` with a single reviewed `0001_baseline` that contains DB-05, 07, 12, 14–19, 27, 29, the naming convention, server defaults and UUIDv7 defaults.

- **Why not incremental:** the changes rename every constraint, replace both enum types, drop or retype more than six columns and add constraints and indexes. As expand/contract migrations that is more than ten revisions of pure churn for a boilerplate that, by assumption, carries no production data.
- **Cost:** any existing database at `37a0d49f8bbc` must be recreated, or stamped after a manual conversion. "Never edit an applied migration" is respected: the old revision is deleted and replaced, not edited. This is **breaking** and must be stated in the changelog.
- **Downstream projects** that already deployed with data must not take the baseline. They need an incremental path instead; this needs your answer (Q1).

## 9. Proposed implementation order

1. **Hotfixes, no schema change, independent:**
   - DB-09: secret logging.
   - DB-01: fast-path revocation check.
   - DB-07, service side: truncate and bound header values.
   - DB-04: atomic counter `UPDATE`.
2. **Tooling foundation:**
   - DB-02: lazy engine, models import `Base` from `common.models`, model registry.
   - DB-22 / Known issue 11: `env.py`.
   - DB-08: tests on asyncpg + PG 18, Alembic-only schema, round-trip and `alembic check` tests.
   - DB-20: engine timeouts.
3. **Model base and D10:** §7.1 mixins, naming convention, UUIDv7 (after the D10 sign-off).
4. **New baseline migration:** DB-05, 07, 11, 12, 14–19, 25, 27, 29; README Data model and `database_schemas/` (DB-30).
5. **Unit of work:** DB-06, DB-21, DB-13 (crud stops committing, `transaction()` helper, `IntegrityError` mapping, routers stop swallowing exceptions; the latter is Phase 2 and touches the backend-engineer's area).
6. **Concurrency-correct auth writes:** DB-03 and DB-10 (DB-arbitrated rotation, Redis removed or hash-keyed), DB-15 (atomic token consumption), DB-28.
7. **Lifecycle:** DB-23 (cleanup and retention cron, erasure anonymisation, audit `REVOKE`), DB-26 (worker pool settings).
8. **SQLAlchemy 2.1.8 and `requires-python >= 3.14`:** DB-24, once step 2 makes the suite trustworthy.

## 10. Open points and clarifying questions

1. Does any environment, including downstream projects, hold real data at revision `37a0d49f8bbc`? **Why it matters:** baseline vs. incremental migrations. **My default:** no, so use the new baseline.
2. Target PostgreSQL major: 18 everywhere (tests, compose, Swarm)? **Why it matters:** `uuidv7()` server default, test parity. **My default:** 18.
3. Usernames case-insensitive (`lower(username)` unique)? **My default:** yes, to prevent impersonation.
4. Audit retention period (GDPR/ISO 27001 A.8.15)? **My default:** 365 days, configurable.
5. Concurrent refresh from several tabs: hard reuse detection, or a short grace window (for example 10 s, where the previous token returns the same successor)? **My default:** no grace window, because the frontend serialises refresh with Web Locks.
6. Is exposing the creation time in `users.id` acceptable (D10 opt-out)? **My default:** acceptable.

Affected known issues: 2 and 11 (confirmed, extended), 5 (DB-01 must be fixed first or revocation stays ineffective). Roadmap Phase 4 items 1, 2, 4, 5, 6 are confirmed; item 3 (UUIDv7) has a recommendation above.
