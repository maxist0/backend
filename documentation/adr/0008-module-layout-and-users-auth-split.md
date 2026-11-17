---
status: accepted
date: 2026-11-16
decision-makers: project owner
---

# Fixed module layout, a users module separate from auth, and core consolidation

Amends [ADR 0001](0001-core-and-module-contract.md).

## Context and Problem Statement

The first core/modules split (S1.2 of the 2026-11 cleanup) left three problems:

* The former auth and user packages were merged into one `auth` module. The user's own data (profile, and later settings, avatar, account deletion) is a separate concern that will grow. Hiding it inside auth makes `auth` the place every user feature lands.
* Modules had no fixed shape. `auth` contained `jwt.py`, `constants.py` and `users.py`, which no other module would have. A reader could not predict where code lives.
* The core had small standalone files (`db_errors.py`, `utils.py`) with no clear home. Moving `db_errors` into `database.py` would create an import cycle: `log_unexpected_error` in `logging.py` needs the DB error helpers, and `database.py` already imports `logging.py`.

## Decision Drivers

* A new developer predicts where code lives without reading it.
* Modules depend on each other in one direction only, so no import cycles.
* URLs describe resources; module boundaries describe responsibilities. The two need not coincide.
* No standalone helper files in the core.

## Considered Options

For the user entity:

* `users` owns `User`, profile and `CurrentUser`; `auth` depends on `users`.
* `auth` owns `User` and `CurrentUser`; `users` only holds profile and settings routes and depends on `auth`.

For the module shape:

* A fixed vocabulary of file names; a module creates only the files it needs.
* Every module always has every file, empty ones included.
* A fixed vocabulary plus a scaffolding command.

For password and email change of the signed-in user:

* Paths under `/users/me/...`, code in `auth` (it needs credentials and verification).
* Paths under `/auth/...`.
* Paths under `/users/me/...`, code in `users`. This needs `users` to import `auth`, which is a cycle.

## Decision Outcome

**Users and auth.** `users` owns the `User` model, the profile endpoints and the `CurrentUser`/`OptionalUser` dependencies. `auth` is how a user proves identity: credentials, sessions, email tokens, the audit log and the flows that create or use them. The dependency order is `core ← users ← auth ← app modules`. Token decoding moves to the core (`core/security.py`), so `users` can resolve the current user without importing `auth`.

**URLs follow the resource, code follows the responsibility.** Endpoints for the signed-in user are under `/users/me/...`. Flows for signed-out users are under `/auth/...` (register, login, refresh, logout, password forgot and reset, email verification). `PUT /users/me/password` (204) and `PUT /users/me/email` (202, takes effect after verification, requires the current password) are served by the `auth` module with the OpenAPI tag `users`.

**Fixed module vocabulary.** A module may contain only these names: `__init__.py`, `router.py`, `schemas.py`, `service.py`, `crud.py`, `models.py`, `dependencies.py`, `errors.py`, `tasks.py`, `admin.py`, `templates/`. It creates only the files it needs. A unit test enforces the vocabulary. Templates live directly in `templates/`, never named `base.html`.

**Core consolidation.** The core files are `config`, `database` (with `utcnow`), `errors` (the `AppError` family, RFC 9457 handlers, DB error inspection, `log_unexpected_error`), `security` (JWT, Argon2 password hashing, token generation and hashing), `logging` (setup and `get_logger`), `middleware`, `health`, `cache`, `tasks`, `mail`, `admin`, `module`, plus `templates/base.html`. `db_errors.py`, `utils.py`, `exceptions.py`, `responses.py`, `schemas.py` and `metrics.py` go away.

### Consequences

* Good, because every module looks the same and a reader knows where to look.
* Good, because the module graph is acyclic and the direction is documented.
* Good, because user features (settings, deletion) get a natural home outside auth.
* Good, because password hashing and JWT are reusable infrastructure, not hidden in a module.
* Bad, because two modules contribute endpoints under `/users/me`. The OpenAPI tag keeps them together for API users, but a developer looks in `auth/router.py` for the password and email endpoints.
* Bad, because `auth` reads and writes the `users` table (lock state, verification flag) through the `users` models. A module may use the models and services of a module it depends on; never the reverse.

## More Information

* Conventions: [AGENTS.md → Module layout](../../AGENTS.md#module-layout-target) and [Core vs modules](../../AGENTS.md#core-vs-modules-target).
* Implemented in S1.2 (structure) and S1.3 (paths and methods) of the 2026-11 cleanup.
* The pending email change is completed through `POST /auth/email/verify`, which consumes the `CHANGE_EMAIL` token, switches the account to the new address and verifies it.
