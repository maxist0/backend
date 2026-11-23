---
status: accepted
date: 2026-11-23
decision-makers: project owner
---

# Idempotency keys in PostgreSQL, in the transaction of the business write

Resolves open decision D9.

## Context and Problem Statement

A client that loses a response cannot tell whether its request ran. Retrying a registration, a password reset or an email change could then create a second account, send a second mail, or answer a valid link with 400 `invalid_token` because the first request already used the token. The frontend already sends a UUIDv7 key with every non-GET request, one key per intended operation across its retries, but the API ignored it. For the account-link calls the frontend therefore turned its retries off.

The IETF draft [draft-ietf-httpapi-idempotency-key-header-09](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-idempotency-key-header-09) (2026-11-21) defines the `Idempotency-Key` request header. A retry with the same key gets the result of the first request; the same key with another payload gets 422; a request while the first one is still running gets 409; a malformed or missing key gets 400. Stripe's API v1 has worked this way for years ([docs](https://docs.stripe.com/api/idempotent_requests), checked 2026-11-23).

The question was where the key lives and how a module uses it, without editing the core ([ADR 0001](0001-core-and-module-contract.md)).

## Decision Drivers

* A request runs at most once, also after a crash; an older roadmap line had a Redis-backed store with a TTL.
* Modules mark a route and write nothing else; the store is an implementation detail of the core.
* Routes whose response carries credentials never store or replay it.
* Bodies hold passwords and tokens: nothing derived from them may be testable offline.
* No new dependency.

## Considered Options

* PostgreSQL, the key row inserted in the request's own transaction.
* Redis: a lock while the request runs and the response stored with a TTL.
* PostgreSQL for the result plus a Redis lock for requests in flight.

## Decision Outcome

Chosen option: "PostgreSQL in the request's own transaction". The row and the business change commit together or not at all, so a crash can never leave a business change without its key. Redis would need a second store whose state can disagree with the database after a crash.

**Interface.** `idempotent(scope=...)` in `app.core` builds a route dependency; the users module binds it to the signed-in user and exports it as `Idempotent`. A module writes `dependencies=[rate_limit(...), Idempotent]` and nothing else: at least one rate limit before the marker (every keyed request writes a row), and the marker last, because a replay skips everything after it. The marker checks only the account state of the bearer token's user, so an authorisation check that must also hold for a retry is a route-level dependency before it. It fits only routes whose answer depends on nothing but user, method, path, query and body. Convention tests over `MODULES` enforce the order. The OpenAPI step adds the header and the error codes to every marked operation. Only a FastAPI dependency can write into the request's `DbSession` (FastAPI caches `get_db` per request) without a second marker. A route class would have to be set per router and still need a per-route marker, and FastAPI 0.163 keeps a route's own class when it is included elsewhere.

**Flow.**

1. The guard parses the header. It accepts the draft's quoted string and a bare token: 16 to 255 characters of `A-Z a-z 0-9 + / = _ . : ~ -`. A missing header means normal processing; anything else malformed, empty or repeated gets 400 `idempotency_key_invalid`.
2. It computes the fingerprint: HMAC-SHA256 over the method, path, query string and body, length-prefixed. The HMAC key is derived from `JWT_SECRET_KEY` under its own label (`derive_key`, shared with the rate-limit digests).
3. It inserts `(scope, key, fingerprint)` with `INSERT … ON CONFLICT (scope, key) DO NOTHING`, under `SET LOCAL lock_timeout = '500ms'` and `SET LOCAL lock_timeout TO DEFAULT` afterwards. A parallel request with the same key waits on the unique index until the first one commits or rolls back. After 500 ms it gets 409 `idempotency_key_in_use` with `Retry-After: 1`. The business statements keep the session's own lock timeout. Both PostgreSQL behaviours are proven by tests on the pinned PostgreSQL 18.7 image.
4. A key that exists already:
   * another fingerprint gets 422 `idempotency_key_reused`;
   * a stored response is replayed with `Idempotent-Replayed: true`;
   * a row without response younger than 60 s gets 409 `idempotency_key_in_use`;
   * an older row without response gets 409 `idempotency_outcome_unknown`. Its request committed but its response was lost, and the request is never run again.
5. The request middleware hands a copy of every response message to a recorder the guard registered. After the route, the guard stores status, body and the allowlisted headers (`content-type`, `location`, `www-authenticate`) and commits. A route that wrote nothing thereby commits its row together with the response.

**What is stored.** A response is stored when the key row was committed, by the service or by the guard, and its status is below 500 and not 429:

* An error that rolled everything back leaves no row, so a retry runs again (Stripe's "execution did not begin").
* An error after an early commit is stored. Examples are a wrong password whose failure counter was committed, or a spent link of a blocked account. The retry replays it, so a failed attempt is not counted twice.
* A 429 deletes a committed row: limits refuse before the work they protect ([ADR 0013](0013-rate-limiting-in-the-app-on-redis.md)), so the retry must run.
* A 5xx keeps a committed row without response. The outcome is unknown, and a replayed 500 would claim a failure that may not be one. Unlike Stripe, the retry gets `idempotency_outcome_unknown` and does not run again.
* A response with `Set-Cookie`, a body above 64 KiB, an incomplete body or pending ORM changes is never stored. The guard rolls back and logs `idempotency.response_not_stored`. 409 rather than a 5xx for a lost result, because transports retry a 5xx with the same key.

**Scope.** The signed-in user (`user:<id>`, with the checks of `OptionalUser`) or, for anonymous requests, the route (`route:<METHOD> <template>`). The guard reads the bearer token like the OAuth2 scheme does, but not through it, so anonymous routes are not marked as secured in OpenAPI.

**Retention.** `IDEMPOTENCY_RETENTION_HOURS` (default 24, 1 to 168). The core task `maintenance.idempotency_cleanup` runs hourly (`23 * * * *`) and deletes older rows in batches of 1000 with `SKIP LOCKED`, at most 100 batches per run like `auth.cleanup`. A run that stops at the cap logs `capped=true` at warning level and counts `idempotency_cleanup_capped_total`, which has an alert query. A key is therefore kept at least 24 hours and gone within 25. An expired row is not taken over; clients use fresh keys.

**Table.** `idempotency_keys` is owned by the core (`app/core/idempotency.py`). A separate module would form an import cycle with `PATCH /users/me`, and the `users` module would mix an unrelated table into the user entity. The table has no foreign key to `users`, because the core may not depend on a module's table.

**Built-in routes.** `POST /auth/register`, `/auth/password/forgot`, `/auth/password/reset`, `/auth/email/verify`, `/auth/email/verify/resend`, `PUT /users/me/email`, `PUT /users/me/password` and `PATCH /users/me` (which got a rate limit of 30 per minute per user for this). Forgot password commits at the same point for known and unknown addresses: the commit decides whether a parallel request with the same key gets 409 or a replay, and must not tell the two apart. Never on login and refresh (tokens and `Set-Cookie`), logout and logout-all (idempotent by nature) or the 2FA stubs.

### Consequences

* Good, because a retried request never runs twice, also after a crash or a lost response, and a token link can be retried safely.
* Good, because a module marks a route with one name, and its service stays unchanged.
* Good, because nothing new runs outside PostgreSQL and no dependency was added.
* Bad, because a service of a marked route must not roll back the request's transaction on its success path: the rollback discards the key row. `reset_password` now ends its read transaction before Argon2 with `commit()`. A lost row is logged as `idempotency.key_row_lost`.
* Bad, because the guard commits after the route: pending ORM changes a route left on purpose would be written. The guard refuses to store and rolls back when the session has any.
* Bad, because a keyed request costs a few more statements (lock timeout, insert, store) and a parallel duplicate holds a pooled connection for up to 500 ms.
* Bad, because stored bodies can hold personal data (the `UserResponse` of a registration or a profile change) for up to 25 hours, in the same database as the users.
* Bad, because a request that commits early and then runs longer than 60 s is answered `idempotency_outcome_unknown` to a retry, although it may still finish.
* Neutral, because rotating `JWT_SECRET_KEY` changes every fingerprint: a retry in flight across the rotation gets 422 `idempotency_key_reused`. Access tokens end at the same moment anyway.
* Bad, because the fingerprint key is derived from `JWT_SECRET_KEY` (owner decision: no separate secret). Whoever obtains both that secret and a copy of the table can test password guesses against the stored fingerprints of anonymous requests offline, at HMAC speed instead of Argon2 speed: the registration's password, a reset's new password together with its token. This affects the rows within the retention (24 hours). The same leak already lets the attacker sign access tokens for every account.

## Pros and Cons of the Options

### Redis lock and TTL store

* Good, because no database write is added to a request.
* Bad, because the key and the business change are not atomic: a crash after the commit leaves no key (the retry runs again), or a stored key without the change.
* Bad, because a Redis outage would either switch the protection off or fail every keyed request.

### PostgreSQL result plus Redis lock

* Good, because a parallel request is answered without a database wait.
* Bad, because two stores must agree; the unique index already serialises parallel requests.

## More Information

Code: `src/app/core/idempotency.py`, the recorder hook in `src/app/core/middleware.py`, `Idempotent` in `src/app/modules/users/dependencies.py`, migration `a575b88f3e52`. How a module uses it: [modules.md](../modules.md#making-a-route-idempotent). Contract: [api.md](../api.md#idempotency-keys). Checked on 2026-11-23 against FastAPI 0.163.1 (`routing.py` `request_response`, `dependencies/utils.py` `solve_dependencies`), Starlette 1.10.0, SQLAlchemy 2.1.8, psycopg 3.3.9 and PostgreSQL 18.7.
