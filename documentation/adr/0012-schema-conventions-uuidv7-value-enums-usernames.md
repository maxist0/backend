---
status: accepted
date: 2026-11-22
decision-makers: project owner
---

# Schema conventions: UUIDv7 keys, model mixins, constraint names, checked VARCHAR enums, case-insensitive usernames

Resolves open decision D10.

## Context and Problem Statement

Before this decision every model repeated its own primary key (`uuid.uuid4`, generated in Python) and its own `created_at`/`updated_at` with Python-side defaults and no server default. Constraint names were PostgreSQL's defaults (`users_email_key`), and the auth service matched those strings to tell a duplicate username from a duplicate email. The two enum columns were native PostgreSQL enum types that stored the Python member names (`LOGIN_SUCCESS`). Usernames were unique only in their exact casing, so `alice` and `Alice` could be two accounts.

Open decision D10 asked which primary key strategy the boilerplate uses: UUIDv4, UUIDv7 or bigint. Modules built on the boilerplate copy whatever the built-in modules do, so the conventions must be settled once and offered by the core.

## Decision Drivers

* A module author must not edit the core to follow the conventions ([ADR 0001](0001-core-and-module-contract.md)).
* Ids are exposed in the API and must not be guessable or enumerable.
* Insert-heavy tables should not scatter writes across the primary key index.
* Rows written outside the ORM (SQL scripts, the test fixtures) must get correct timestamps.
* Constraint names must be identical in every database, so migrations and code can refer to them.
* An enum value must be removable without rebuilding a type and rewriting the table.
* Two accounts must not differ only in the casing of their usernames.

## Considered Options

* Primary key: UUIDv4 in Python (as before); UUIDv7 in Python (`uuid.uuid7()`, Python 3.14); UUIDv7 in PostgreSQL 18 (`uuidv7()` server default); bigint identity.
* Enums: native PostgreSQL enum types; VARCHAR with a CHECK constraint; a lookup table.
* Username uniqueness: unchanged (case-sensitive); a unique index on `lower(username)`; the `citext` extension; lower-casing usernames before storing them.

## Decision Outcome

**Primary keys: UUIDv7 generated in Python.** The core mixin `UUIDPrimaryKey` declares `id` with `default=uuid.uuid7`. There is no server default: the application knows the id before the flush, exactly as with v4. New ids of other kinds (session `family_id`, email token ids) use `uuid.uuid7()` too. The request id and the JWT `jti` stay UUIDv4: they are never stored as keys, and they carry no creation time. A module that must not reveal creation times in its ids declares its own `id` with `default=uuid.uuid4` instead of the mixin ([modules.md](../modules.md#step-2-the-model)).

**Timestamps: two mixins.** `CreatedAt` adds `created_at`; `Timestamps` adds `created_at` and `updated_at`. Both are `timestamptz`, NOT NULL, with `server_default=now()`. The ORM reads `created_at` back with `INSERT ... RETURNING`. It sets `updated_at` on every UPDATE it emits from the application clock (`onupdate=utcnow`): a database-side value would leave the attribute expired, and an async session cannot reload it implicitly. Rows that never change (audit log, email tokens, sessions) use `CreatedAt` and get no `updated_at`. There is no `version` column and no soft delete.

**Constraint names: SQLAlchemy's naming convention** on `Base.metadata`: `pk_<table>`, `uq_<table>_<column>`, `fk_<table>_<column>_<referred table>`, `ck_<table>_<name>`, `ix_<column label>`. The auth service maps a unique violation by name through constants in `app.modules.users.models` (`EMAIL_UNIQUE_CONSTRAINT`, `USERNAME_UNIQUE_INDEX`).

**Enums: VARCHAR plus a CHECK constraint that stores the values.** The core helper `value_enum(EnumClass)` returns `Enum(..., native_enum=False, create_constraint=True, length=64, values_callable=...)`. The column stores `member.value` (`login_success`), and the CHECK constraint `ck_<table>_<lower-case class name>` limits it to the current values. Adding or removing a value is a migration that replaces the CHECK constraint. Autogenerate does not detect that, so an integration test compares every enum column's CHECK constraint with the model.

**Usernames: unique regardless of case, casing preserved.** A unique index `uq_users_lower_username` on `lower(username)` replaces the case-sensitive unique constraint. The login lookup and the registration pre-check filter on `lower(username) = lower(:name)`, the index's expression. A username can contain only ASCII letters, digits, `-` and `_` (request schema, matched against the whole value), and a login name outside that pattern is treated as an unknown user without a lookup. So `lower()` only ever sees ASCII and does not depend on the database collation, which would fold some non-ASCII letters onto ASCII ones (the Kelvin sign onto `k`).

**Migration baseline.** The schema change replaced the three earlier revisions with one reviewed baseline. No data existed on any of them; existing local databases must be recreated.

### Consequences

* Good, because a new module gets the key, the timestamps, the constraint names and the enum storage from four imports, without a core edit.
* Good, because UUIDv7 ids are time-ordered, so new rows are appended to the right end of the primary key index ([RFC 9562, section 6.11](https://www.rfc-editor.org/rfc/rfc9562.html#section-6.11)), and they stay hard to guess: Python's `uuid.uuid7()` fills the bits after the timestamp with a counter that starts at a random value every millisecond and 32 fresh random bits per id.
* Good, because constraint names are the same in every database: migrations can drop a constraint by name, and the 409 mapping no longer depends on PostgreSQL's default names.
* Good, because an enum value can be removed by replacing a CHECK constraint, without the type rebuild and table rewrite a native enum needs ([PostgreSQL 18, section 8.7.4](https://www.postgresql.org/docs/18/datatype-enum.html#DATATYPE-ENUM-IMPLEMENTATION-DETAILS)). The stored values match the API and log names (`login_success`), and the downgrade leaves no type behind.
* Good, because `alice` and `Alice` can no longer be two accounts; a user may log in with any casing and keeps the casing chosen at registration.
* Bad, because a UUIDv7 reveals when its row was created, to the millisecond. User ids appear in the API and in access tokens, so they reveal the registration time to whoever sees them. RFC 9562 calls this "a very small attack surface" (section 8) and recommends UUIDv4 where ids are used for security operations; no id here grants access on its own.
* Bad, because ids are strictly ordered only within one process; ids of several processes interleave by their clocks. Order by `created_at` where the order matters.
* Bad, because autogenerate does not see a changed enum value; the CHECK comparison test is the guard.
* Neutral, because `value_enum` uses a VARCHAR(64) by default, so a longer future value needs no column change, only a new CHECK constraint.

### Confirmation

* `tests/integration/core/test_migrations.py`: upgrade, downgrade to base (no table or type left), upgrade, `alembic check`; every enum column's CHECK constraint holds exactly the model's values; every constraint name follows the convention.
* `tests/unit/core/test_model_base.py`: the convention, the mixins and `value_enum`.
* `tests/integration/modules/auth/test_auth.py`: new rows get UUIDv7 ids; enum columns store values; a username that differs only in case is refused with 409; login ignores the casing.

## Pros and Cons of the Options

### UUIDv4 in Python (as before)

* Good, because ids reveal nothing.
* Bad, because random keys spread inserts over the whole primary key index.

### UUIDv7 in PostgreSQL 18 (`uuidv7()`)

* Good, because rows written outside the ORM get an id as well.
* Bad, because the application would have to flush before it knows an id, and the Swarm stack still runs PostgreSQL 16, which has no `uuidv7()`.

### bigint identity

* Good, because it is the smallest key.
* Bad, because sequential ids are enumerable in an API and cannot be generated before the insert.

### Native PostgreSQL enum types

* Good, because the type documents the values and stores 4 bytes per row.
* Bad, because a value cannot be removed without rebuilding the type and rewriting the table, and a new value cannot be used in the transaction that adds it.

### Lookup table

* Good, because values become rows that can carry labels.
* Bad, because every insert needs the key of a row that is fixed in code anyway; it is machinery without a user today.

### `citext` for usernames

* Good, because comparisons are case-insensitive without `lower()` in queries.
* Bad, because it needs an extension in every database and changes the column type for every reader.

### Lower-casing usernames before storing them

* Good, because a plain unique constraint suffices.
* Bad, because the user's chosen casing is lost.

## More Information

* Owner decision of 2026-11-22 (review Phase 2, step B1.1). In the same step the session family got an absolute lifetime (`SESSION_MAX_LIFETIME_DAYS`, [authentication.md](../authentication.md#tokens)), which closed README known issue 26.
* Sources, checked 2026-11-22: [RFC 9562](https://www.rfc-editor.org/rfc/rfc9562.html) (sections 5.7, 6.11, 8); [Python 3.14 `uuid.uuid7`](https://docs.python.org/3.14/library/uuid.html#uuid.uuid7); [PostgreSQL 18 enum types](https://www.postgresql.org/docs/18/datatype-enum.html); [SQLAlchemy 2.1 constraint naming conventions](https://docs.sqlalchemy.org/en/21/core/constraints.html#configuring-constraint-naming-conventions).
* Conventions for code: [AGENTS.md → Database and migrations](../../AGENTS.md#database-and-migrations); schema reference: [database.md](../database.md).
