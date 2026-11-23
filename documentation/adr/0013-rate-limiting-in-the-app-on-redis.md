---
status: accepted
date: 2026-11-23
decision-makers: project owner
---

# Rate limiting in the app on Redis, and a request body limit

Resolves open decision D4.

## Context and Problem Statement

The API had no rate limits. The only controls against credential guessing were the per-account timed lock (`LOGIN_MAX_ATTEMPTS`, `LOGIN_LOCKOUT_SECONDS`) and a per-process admission limiter in front of Argon2. Neither bounds how often one client may try, how many accounts it may probe, how many reset mails it may trigger for one address, or how many timing samples it may collect (known issues 17 and 23). Request bodies had no size limit either (OWASP API4:2023, Unrestricted Resource Consumption).

Open decision D4 asked where rate limits live: in the app (on Redis) plus the proxy, or in the proxy only. A proxy sees addresses, not accounts, email addresses or session families, and the boilerplate must protect itself in every deployment.

## Decision Drivers

* Limits per account, email address and session family, not only per address; a refused request must not reveal whether an account exists.
* A refused login costs no database lookup, no admission slot and no Argon2 evaluation.
* No new dependency: `limits` 5.8.0's `redis` extra requires redis-py < 8 (we run 8.1), and its coredis atomicity fix is unreleased; slowapi and fastapi-limiter build on `limits` or older stacks.
* Modules use the limits without editing the core ([ADR 0001](0001-core-and-module-contract.md)).
* No identifier in clear text in Redis (GDPR data minimisation): Redis is readable by more people and tools than the database.
* A Redis outage must not take the sign-in down, and it must be visible.

## Considered Options

* In the app: a small core helper on cashews' atomic Redis primitives, plus coarse proxy limits in the deploy round.
* In the app with the `limits` library.
* In the proxy (Traefik) only.

## Decision Outcome

Chosen option: "a small core helper on cashews", because cashews is installed, its Redis primitives are atomic Lua scripts, and the helper stays a few dozen lines.

* `app.core` offers `rate_limit(policy, limit, key=...)`, a route dependency, and `hit(policy, limit, key=...)` for limits keyed by request data inside a service or dependency. `by_ip` (core) and `by_user` (`app.modules.users`) are the route keys. Policies live in code next to the routes, for example `rate_limit("auth.login.ip", "20/minute", key=by_ip)`.
* Algorithm: a sliding-window log per key (cashews `slice_incr`, a sorted set of timestamps, one Lua script per hit). A limit of n allows n + 1 entries, so the result tells an accepted from a refused hit; the refused hit is recorded too, so a client that keeps sending stays refused until it pauses. The key expires one window after its newest entry.
* A refusal is `RateLimitedError`: RFC 9457 problem, status 429 (RFC 6585), `code` `rate_limited`, and `Retry-After` in whole seconds (RFC 9110 section 10.2.3): the key's TTL plus one second (Redis rounds the TTL), at most the window. It is the time until the log is empty, so it is never too early. The IETF `RateLimit` and `RateLimit-Policy` headers are not sent while the draft still changes.
* Keys: `rate_limit:<policy>:<digest>`, the digest being 128 bits of HMAC-SHA256 over the policy and the identifier. The HMAC key is derived from `JWT_SECRET_KEY` with a fixed label (domain separation) instead of a new secret: no new setting, startup guard entry or deployment secret, and the JWT key is already required to be strong. Identifiers are normalised by the caller as the lookup compares them (lower-case usernames and email addresses). IPv6 clients are counted per /64 network, because a host picks addresses inside its /64 freely (RFC 8981).
* The client address is only `request.client.host`, which uvicorn fills from `X-Forwarded-For` for the proxies in `FORWARDED_ALLOW_IPS`; the API never parses the header itself.
* Redis outage: fail-open. The rate-limit store is a second cashews `Cache` on the same server and database as the cache, set up with `suppress=False`, so a Redis error raises (cashews' default `SafeRedis` would log every failed command with a traceback and return `None`). The check lets the request through, increments `rate_limit_fail_open_total{policy}` and logs `rate_limit.fail_open` at most once a minute per process. Connect and pool waits are bounded to 1 s, commands by cashews' 1 s socket timeout.
* Metrics: `rate_limit_rejections_total{policy}` and `rate_limit_fail_open_total{policy}`; policy names are fixed in code.
* `RATE_LIMIT_ENABLED` (default `true`) switches every check off; it is meant for diagnosis.
* The account lock, the lock mail and the admission limiter stay as they are; the limits run in front of them.
* Body limit: `MAX_REQUEST_BODY_BYTES` (default 1 MiB, bounded to 1 KiB to 100 MiB), enforced in the single request middleware ([ADR 0005](0005-single-asgi-request-middleware.md)): a declared `Content-Length` above it is answered before the app runs, and a body without one (chunked) is cut off once the bytes read pass it. The answer is a 413 problem with the framework code `content_too_large` (RFC 9110 section 15.5.14).
* Not in scope: request timeouts and a global per-address limit. Both belong to the proxy (deploy round).

Policies of the built-in routes (owner-approved values):

| Route | Policy | Key | Limit |
|---|---|---|---|
| `POST /auth/login` | `auth.login.ip` | address | 20 per minute |
| `POST /auth/login` | `auth.login.ip_username` | address and lower-case username | 10 per 15 minutes |
| `POST /auth/login` | `auth.login.ipv6_48` | IPv6 /48 (IPv4 not counted) | 100 per minute |
| `POST /auth/register` | `auth.register.ip` | address | 5 per hour |
| `POST /auth/password/forgot` | `auth.password_forgot.ip` | address | 5 per 15 minutes |
| `POST /auth/password/forgot` | `auth.password_forgot.ipv6_48` | IPv6 /48 (IPv4 not counted) | 25 per 15 minutes |
| `POST /auth/password/forgot` | `auth.password_forgot.email` | lower-case email address | 3 per hour |
| `POST /auth/password/reset` | `auth.password_reset.ip` | address | 10 per 15 minutes |
| `POST /auth/email/verify` | `auth.email_verify.ip` | address | 10 per 15 minutes |
| `POST /auth/email/verify/resend` | `auth.email_verify_resend.user` | user | 3 per hour |
| `POST /auth/email/verify/resend` | `auth.email_verify_resend.email` | recipient address | 3 per hour |
| `POST /auth/refresh` | `auth.refresh.ip` | address | 60 per minute |
| `POST /auth/refresh` | `auth.refresh.family` | session family, live tokens only | 10 per minute |
| `PUT /users/me/password` | `auth.password_change.user` | user | 5 per 15 minutes |
| `PUT /users/me/email` | `auth.email_change.user` | user | 5 per 15 minutes |
| `PUT /users/me/email` | `auth.email_change.new_email` | requested new address, all accounts | 3 per hour |
| `POST /admin/login` | `admin.login.ip` | address | 10 per 15 minutes |

The login's per-username limit and the forgot-password limit per address are counted before any lookup, so a 429 is the same for existing and unknown accounts.

### Consequences

* Good, because guessing, account probing, mail flooding and timing sampling are bounded per address, per account and per email address, with no new dependency.
* Good, because a refused login costs one Redis round trip and nothing else.
* Good, because a module limits a route with one line and needs no core edit.
* Good, because Redis holds no address, username or email address.
* Bad, because a third connection pool to Redis exists (client, cache, rate-limit store), each up to `CACHE_MAX_CONNECTIONS` per process.
* Bad, because the limits are off while Redis fails; the metric and the log line make it visible, and the account lock and admission limiter still apply. Under an evicting Redis policy a full Redis drops the logs without any signal, so the counters need their own Redis instance (deploy round).
* Bad, because per-key limits can be turned against their owner: anyone can exhaust the forgot-password limit of a known address, or the login limit of a username from their own address. The account holder still signs in, and from another address the per-username limit is separate.
* Bad, because clients behind one NAT share the per-address limits.
* Neutral, because rotating `JWT_SECRET_KEY` changes every digest: all windows start afresh, and the old keys expire on their own.
* Neutral, because the log stores the timestamp as the entry, so two hits of one key in the same microsecond count once.

### Security review follow-up (2026-11-23)

A security review of the first implementation found the points below; the owner approved every fix on the same day, before the first commit.

* **A limit must not hide reuse detection.** The family limit was counted before the rotation gate, so a thief who kept the family's window full could turn the victim's replay of a rotated token into a 429 instead of a family revocation (RFC 9700 section 4.14.2). Only a live token is counted now; a rotated one always reaches the reuse check.
* **No catch-all proxy trust.** Outside `dev` and `unittest` the startup guard refuses `FORWARDED_ALLOW_IPS` with `*`, `0.0.0.0/0` or `::/0`: uvicorn would take any client's `X-Forwarded-For` as its address.
* **Short-circuit on outage.** After a store error each process skips Redis for 5 s and fails open at once, so a hanging Redis delays one request per process every 5 s by about a second instead of every request. 5 s keeps the limits off for at most 5 s after Redis returns; 10 s would halve the probes but double that gap. A refusal whose TTL read fails is still a 429, with the whole window as `Retry-After`.
* **IPv6 sites.** Login and forgot-password also count per /48, at five times the per-/64 limit (100 per minute, 25 per 15 minutes). A /48 is a site allocation (RIPE-690) with 65,536 /64s; five times leaves room for a few busy subnets of one organisation while bounding a client that rotates through them. IPv4 clients are not counted by these policies (the key is `None`): their per-address policy already bounds them, and a second, looser count would only cost a Redis call.
* **Per-recipient mail limits.** 3 email-change links an hour per new address (after the password check, before the address is looked up, so a 429 does not reveal registration) and 3 verification mails an hour per address, across accounts.
* **Admin login.** 10 attempts per 15 minutes per address, before the credential compare; a refused attempt looks like a failed login.
* **Visibility.** `Retry-After` and `X-Request-ID` are exposed to the allowed CORS origins; `RATE_LIMIT_ENABLED=false` outside `dev` and `unittest` logs `rate_limit.disabled` at startup.
* **Parallel hits.** The parallel test the review asked for found an over-admission: `slice_incr` counts only entries up to the hit's own client-side timestamp, so hits that reach Redis in another order than their timestamps miss each other (3 to 7 of 10 parallel requests passed a limit of 2). Hits of one key are now serialised per process (an `asyncio.Lock` per key, held while the timestamp is taken and the script runs), which makes a burst within one process exact. Across processes the inversion window is clock skew plus network jitter; the owner accepted that remainder (README known issue 34) instead of an own Lua script with the server clock or a fixed window.
* **Redis behaviour, checked on 7.4.12.** A full Redis with `noeviction` keeps counting: Redis refuses an out-of-memory write inside a script only if it is the script's first write, and the log script writes first with `ZREMRANGEBYSCORE`. Error replies such as `READONLY` or `WRONGTYPE` fail open like connection errors.

## Pros and Cons of the Options

### The `limits` library

* Good, because it offers several algorithms and storage backends.
* Bad, because its Redis extra pins redis-py below 8, and its atomic coredis path is unreleased.
* Bad, because it is a new dependency for a few dozen lines on top of what is installed.

### Proxy only

* Good, because no request reaches the app once a limit applies.
* Bad, because the proxy cannot key by account, email address or session family, and it differs per deployment.

## More Information

Code: `src/app/core/rate_limiting.py`, `src/app/core/cache.py` (`rate_limit_store`), the body limit in `src/app/core/middleware.py`. How a module uses it: [modules.md](../modules.md#rate-limiting-a-route). Sources: OWASP API Security Top 10 2023 (API2, API4), OWASP Authentication Cheat Sheet, NIST SP 800-63B-4 section 3.2.2, RFC 6585, RFC 9110, RFC 9457, RFC 8981. Checked on 2026-11-23 against the installed cashews 7.6.0 (`backends/redis/backend.py`, `_INCR_SLICE`), redis-py 8.1.0 and Starlette 1.10.0.
