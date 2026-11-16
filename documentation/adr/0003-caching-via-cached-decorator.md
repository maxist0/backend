---
status: accepted
date: 2026-11-16
decision-makers: project owner
---

# Caching via a `@cached` decorator

## Context and Problem Statement

The cache layer used cashews as a route cache. In practice it cached `GET /` as a whole response, so a client could receive another client's `request_id`. Supporting it needed middlewares that ran on every request, and it opened a second Redis pool next to the one used by taskiq and the rest of the app.

How does the core offer caching without these costs?

## Decision Drivers

* A cached value must not contain per-request data.
* No cost on requests that do not use the cache.
* One Redis client.

## Considered Options

* A thin `@cached(ttl, key)` and `invalidate(pattern)` wrapper over cashews, caching return values.
* The same, plus an ETag middleware.
* A custom mini cache without cashews.

## Decision Outcome

Chosen option: "a thin `@cached(ttl, key)` and `invalidate(pattern)` wrapper over cashews", because it keeps the library for the hard parts and removes everything else.

* The core offers only `@cached(ttl, key)` and `invalidate(pattern)`. They cache return values of functions, not HTTP responses, on the one Redis client (`RedisDep`).
* There is no ETag middleware, no client cache-control middleware and no route-cache settings (`ROUTE_CACHE_*` are removed).
* Keys must include every input that changes the result, `{user_id}` included for per-user data.
* Mutations call `invalidate(...)` in the service. TTLs come from `Settings`.

### Consequences

* Good, because a cached value cannot leak a `request_id` or another per-request field.
* Good, because requests that use no cache pay nothing.
* Good, because there is one Redis pool.
* Bad, because there is no HTTP-level caching (`ETag`, `304`). A module that needs it builds it for its own case.
* Bad, because a key that misses an input leaks data across users. Review every `key=` for this, and cover it with a test.

## Pros and Cons of the Options

### Also an ETag middleware

* Good, because clients could revalidate cheaply.
* Bad, because the middleware would run on every request, and no route needs it today.

### Custom mini cache without cashews

* Good, because it avoids a dependency.
* Bad, because serialisation, TTL handling and pattern invalidation would become our code, and the wrapper is only a few lines on top of cashews.

## More Information

Implemented in S1 of the 2026-11 cleanup. Conventions: [AGENTS.md → Caching and background jobs](../../AGENTS.md#caching-and-background-jobs-target).
