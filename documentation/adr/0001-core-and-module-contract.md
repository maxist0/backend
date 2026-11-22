---
status: accepted (amended by ADR 0008)
date: 2026-11-16
decision-makers: project owner
---

# Core and module contract

## Context and Problem Statement

The backend is a boilerplate. User management, scaling and the other production features are meant to be a stable core, and someone building a web app should only add modules. In the code before this decision, adding one new module needed six edits in core files. The core was not stable, and the line between core and feature code was unclear.

How does a module plug into the app without any edit to the core?

## Decision Drivers

* The core must not change when an app adds a module.
* One registration point, easy to read and review.
* No machinery for cases that do not exist yet ([simplicity principle](../../AGENTS.md#simplicity-first)).

## Considered Options

* A `Module` dataclass plus an explicit `MODULES` tuple.
* Auto-discovery of packages under `app/modules/`.
* A list of routers only, with models, tasks and admin views wired separately.

## Decision Outcome

Chosen option: "a `Module` dataclass plus an explicit `MODULES` tuple", because it is the only option that is both explicit and complete.

* `src/app/core/` holds the infrastructure. `src/app/modules/` holds the modules. The built-in module `modules/auth` merges the former auth and user packages.
* The core never imports from `app.modules`. Modules import infrastructure from `app.core` (a public facade in `app/core/__init__.py`) and user/auth from `app.modules.auth`.
* `Module(name, router, models=None, tasks=None, admin_views=())` is a frozen dataclass. The router carries its own `prefix` and `tags`. Module resources use `APIRouter(lifespan=...)`.
* The single registration line goes into `MODULES = (auth.module, ...)` in `app/modules/__init__.py`.
* `create_app()` mounts every module router under `settings.API_PREFIX` (default `/api/v1`). The worker imports `module.tasks`, Alembic imports `module.models`, and the admin registers `module.admin_views`.
* Auth offers modules only the `CurrentUser` and `OptionalUser` aliases for now. Roles and permissions (D3), user events and per-module settings are deferred.

> **Correction, 2026-11-22.** [ADR 0008](0008-module-layout-and-users-auth-split.md) split the merged module. Modules import `CurrentUser`, `OptionalUser` and `User` from `app.modules.users`, not from `app.modules.auth`, and the built-in modules are `users` and `auth`. The first registration line is `MODULES = (users.module, auth.module, ...)`. The decision above is unchanged.

### Consequences

* Good, because adding a module is one package plus one line in `MODULES`.
* Good, because a reviewer sees every registered module in one tuple.
* Good, because the one-way import rule can be checked mechanically.
* Bad, because the registration line is manual: forgetting it means the module is silently absent.
* Bad, because the `Module` fields are a contract. A new kind of module hook is a core change and needs its own decision.

## Pros and Cons of the Options

### Auto-discovery

* Good, because there is no registration line.
* Bad, because it is implicit: what runs depends on what happens to be on disk, which makes startup order, test isolation and removal harder to reason about.

### Router-only list

* Good, because it is the smallest possible contract.
* Bad, because models, tasks and admin views still need separate edits, so a module still touches the core.

## More Information

Implemented in S1 of the 2026-11 cleanup. Procedure: [Adding a module](../modules.md); the rules are in [AGENTS.md → Adding a module](../../AGENTS.md#adding-a-module).
