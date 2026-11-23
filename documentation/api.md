# API structure

The routes of the API, the error format and the response conventions. The API contract is owned by the backend; the frontend's view of it is [backend-contract.md](../../frontend/documentation/backend-contract.md). The rules for writing routes are in [AGENTS.md](../AGENTS.md) (Architecture rules, Errors and responses). To add a resource, see [modules.md](modules.md).

Form: reference. The live document is `GET /openapi.json` (off in `prod`); `tests/unit/core/openapi_snapshot.json` is the committed copy.

## Conventions

| Topic | Rule |
|---|---|
| Version | Module routers are mounted under `API_PREFIX` (default `/api/v1`). `/`, `/health`, `/health/ready` and `/metrics` stay unprefixed. |
| Success | A typed Pydantic model, no envelope. A 202 or 204 has no body and no `Content-Type`. |
| Errors | RFC 9457 `application/problem+json` ([ADR 0002](adr/0002-rfc9457-errors-plain-responses-api-v1.md)). |
| Paths | Plural nouns, kebab-case; actions as sub-resources (`/auth/password/reset`). |
| IDs, times | IDs are UUID strings. Timestamps are RFC 3339 in UTC. |
| Auth | `Authorization: Bearer <access token>`. The refresh token travels only in the `refresh_token` cookie (`HttpOnly`, `SameSite=Lax`, path `<API_PREFIX>/auth`). |
| Request id | Every response has `X-Request-ID`; a valid client value is echoed. |
| Request bodies | Pydantic models with `extra="forbid"`: an unknown field is a 422. At most `MAX_REQUEST_BODY_BYTES` (1 MiB by default), else 413. |
| Rate limits | Per policy, 429 `rate_limited` with `Retry-After` ([Limits](#limits)). |
| Retries | Routes that create something or have a side effect accept an optional `Idempotency-Key`; a retry with the same key gets the stored response ([Idempotency keys](#idempotency-keys)). |

## Error format

```json
{
  "type": "about:blank",
  "title": "Unprocessable Content",
  "status": 422,
  "detail": "The request contains invalid input.",
  "code": "validation_error",
  "request_id": "0161c42e-d89a-4892-af69-4e8934eb2fd9",
  "errors": [{ "field": "username", "code": "string_too_short", "message": "String should have at least 3 characters" }]
}
```

- `code` is stable and machine-readable; clients branch on it, not on `detail`.
- `errors[]` appears only on a 422. A body field has no prefix; other sources are prefixed (`query.limit`, `header.x`, `cookie.x`, `path.note_id`).
- Every 401 carries `WWW-Authenticate: Bearer`.
- A 500 carries a generic `detail`, a `request_id` and never the exception text.
- A 429 `rate_limited` carries `Retry-After` in whole seconds: after that wait the same request is accepted again ([Limits](#limits)).
- A route marked idempotent can also answer 400 `idempotency_key_invalid`, 409 `idempotency_key_in_use` (with `Retry-After: 1`), 409 `idempotency_outcome_unknown` and 422 `idempotency_key_reused` ([Idempotency keys](#idempotency-keys)); its OpenAPI operation lists them.
- Statuses that the framework raises get the reason phrase in snake case as `code`: 404 `not_found` for an unknown path, 405 `method_not_allowed`, 413 `content_too_large` for a body above `MAX_REQUEST_BODY_BYTES`.
- Services raise `AppError` subclasses (`BadRequest`, `Unauthorized`, `Forbidden`, `NotFound`, `Conflict`, `RateLimitedError` in `src/app/core/errors.py`); a module sets its own `code` in its `errors.py`. The codes of the auth module are in `src/app/modules/auth/errors.py`; the list with the meaning for clients is in [backend-contract.md](../../frontend/documentation/backend-contract.md#error-codes).

Status codes: 200 read or update, 201 create, 202 accepted for later work, 204 no content, 400 bad input, 401 missing or invalid credentials, 403 authenticated but not allowed, 404 not found, 409 conflict, 413 body too large, 422 validation, 429 rate limited, 500 unexpected.

## Core routes

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | Entry points of the API (`docs_url`, `metrics_url`, `auth_url`) |
| GET | `/health` | Liveness: answers while the process serves requests, checks no dependency |
| GET | `/health/ready` | Readiness: PostgreSQL `SELECT 1` and Redis `PING`; 503 with the failed check, never error text |
| GET | `/metrics` | Prometheus exposition (plain text); not in the OpenAPI document; mounted only with `METRICS_ENABLED` |

## Module routes

All paths below are relative to `API_PREFIX`. "Bearer" means an access token is required.

**users** (tag `users`)

| Method | Path | Status | Auth | Purpose |
|---|---|---|---|---|
| GET | `/users/me` | 200 | Bearer | Profile of the signed-in user |
| PATCH | `/users/me` | 200 | Bearer | Update the profile (`display_name`); at most 30 per minute; key |
| PUT | `/users/me/password` | 204 | Bearer | Change the password; needs the current password; signs out the other sessions; key |
| PUT | `/users/me/email` | 202 | Bearer | Request an email change; needs the current password; a link goes to the new address; key |

`PUT /users/me/password` and `PUT /users/me/email` are served by the `auth` module (they need credentials and email tokens) under the tag `users`.

**auth** (tag `authentication`)

| Method | Path | Status | Auth | Purpose |
|---|---|---|---|---|
| POST | `/auth/register` | 201 | none | Create an account and mail a verification link; key |
| POST | `/auth/login` | 200 | none | OAuth2 password form; returns an access token and the user; sets the refresh cookie |
| POST | `/auth/refresh` | 200 | refresh cookie | Rotate the refresh token, return a new access token |
| POST | `/auth/logout` | 204 | refresh cookie | End this session, clear the cookie |
| POST | `/auth/logout-all` | 204 | Bearer | End every session of the user |
| POST | `/auth/password/forgot` | 202 | none | Mail a reset link; the answer is the same for every address; key |
| POST | `/auth/password/reset` | 204 | token in body | Set a new password; signs out every session; key |
| POST | `/auth/email/verify` | 204 | token in body | Redeem a verification or email-change token; key |
| POST | `/auth/email/verify/resend` | 202 | Bearer | Mail a new verification link; key |
| POST | `/auth/2fa/enable`, `/disable`, `/verify` | 501 | Bearer | Stubs; answer `not_implemented` |

"key": the route accepts `Idempotency-Key` ([Idempotency keys](#idempotency-keys)). Login, refresh, the logouts and the 2FA stubs do not: a response with a token or a cookie is never stored, and the logouts are idempotent by nature.

The flows behind these routes are described in [authentication.md](authentication.md).

## Limits

**Request body.** A body larger than `MAX_REQUEST_BODY_BYTES` (default 1 MiB) is refused with 413 `content_too_large`: at once when `Content-Length` declares it, otherwise (a chunked body) as soon as the bytes read pass the limit. The request middleware enforces it for every route ([ADR 0013](adr/0013-rate-limiting-in-the-app-on-redis.md)). Routes do not list the 413 in their OpenAPI `responses`, like the 500.

**Rate limits.** Each limit is a policy with a key and a sliding window. A request above the limit gets 429 `rate_limited` with `Retry-After` (seconds until the window is free again), and nothing else happens: no lookup, no password check, no mail, no rotation. Refused requests count too, so a client that keeps sending stays refused until it waits `Retry-After`. Routes with a limit list the 429 in their `responses`. While Redis fails the limits are off (known issue 31).

| Route | Key | Limit |
|---|---|---|
| `POST /auth/login` | client address (IPv6 per /64) | 20 per minute |
| `POST /auth/login` | IPv6 /48 network (IPv4 clients are not counted) | 100 per minute |
| `POST /auth/login` | client address and username (any casing) | 10 per 15 minutes |
| `POST /auth/register` | client address | 5 per hour |
| `POST /auth/password/forgot` | client address (IPv6 per /64) | 5 per 15 minutes |
| `POST /auth/password/forgot` | IPv6 /48 network (IPv4 clients are not counted) | 25 per 15 minutes |
| `POST /auth/password/forgot` | email address (any casing, from any client) | 3 per hour |
| `POST /auth/password/reset` | client address | 10 per 15 minutes |
| `POST /auth/email/verify` | client address | 10 per 15 minutes |
| `POST /auth/email/verify/resend` | user | 3 per hour |
| `POST /auth/email/verify/resend` | the address the mail goes to | 3 per hour |
| `POST /auth/refresh` | client address | 60 per minute |
| `POST /auth/refresh` | login session (refresh-token family); only a live refresh token is counted | 10 per minute |
| `PUT /users/me/password` | user | 5 per 15 minutes |
| `PUT /users/me/email` | user | 5 per 15 minutes |
| `PUT /users/me/email` | the requested new address, from all accounts (after the password check, before the address is checked) | 3 per hour |
| `PATCH /users/me` | user | 30 per minute |

- The login and forgot-password limits count the submitted username or address whether or not an account has it, before any lookup, so a 429 tells nothing about accounts. The same holds for the new address of an email change.
- A 429 on refresh leaves the refresh cookie valid; refresh again after `Retry-After`. A refresh token that was already rotated is never refused with 429: it always reaches the reuse check, which revokes its family.
- The client address is the one uvicorn takes from the trusted proxy (`FORWARDED_ALLOW_IPS`; `*` and catch-all networks are refused outside `dev`/`unittest`). IPv6 clients are counted per /64 network, and on login and forgot-password also per /48.
- The limits run in front of the account lock (`LOGIN_MAX_ATTEMPTS`), which is unchanged ([authentication.md](authentication.md#two-kinds-of-lock)).
- `Retry-After` and `X-Request-ID` are exposed to the allowed CORS origins (`Access-Control-Expose-Headers`).
- A replay of a stored response (see below) counts against the route's limits like any request; it does not reach the limits a service counts itself (per email or recipient). A 429 is never stored: after `Retry-After` the same request with the same key runs normally.

## Idempotency keys

A route marked "key" in the tables above accepts the header `Idempotency-Key` ([draft-ietf-httpapi-idempotency-key-header-09](https://datatracker.ietf.org/doc/html/draft-ietf-httpapi-idempotency-key-header-09), [ADR 0014](adr/0014-idempotency-keys-in-postgres-same-transaction.md)). It is optional: without it the request runs as before.

**The key.** One value per intended operation, kept across the retries of that operation, and a new one whenever the request changes (another body, another target). Format: 16 to 255 characters of `A-Z a-z 0-9 + / = _ . : ~ -`, bare (`Idempotency-Key: 019abcde-…`) or as the draft's quoted string (`Idempotency-Key: "019abcde-…"`); both are the same key. A UUID (v4 or v7) fits. Keys compare exactly. Keys belong to the signed-in user, or for a request without a valid access token to the route.

| Situation | Answer |
|---|---|
| First request with the key | Runs normally. Its response is stored when it is not a 5xx or a 429. |
| Same key, same request (method, path, query string, body) | The stored response: same status, body and `Content-Type`, plus `Idempotent-Replayed: true`. Nothing runs again. |
| Same key, another request | 422 `idempotency_key_reused`. |
| Same key while the first request is still running | 409 `idempotency_key_in_use` with `Retry-After: 1`; retry after the wait. A request that holds the key uncommitted makes the second one wait up to 0.5 s first. |
| Same key, the first request committed but its response was lost (a crash, a 5xx after the commit) | 409 `idempotency_outcome_unknown` once the key is 60 s old. The request is never run again: read the current state, and use a new key for a new attempt. |
| Header malformed, empty or sent twice | 400 `idempotency_key_invalid`; nothing runs. |

- Every marked route has a rate limit, counted before the key is written; a 429 leaves no key.
- A request that fails before it wrote anything (a validation error, a 409 `user_exists`, a 401) leaves no key: a retry runs again. A refusal after a write is stored and replayed, for example a wrong current password, whose failure counter was written.
- A replayed problem keeps the `request_id` of the original request in its body; the `X-Request-ID` header is the retry's own.
- A key is kept for `IDEMPOTENCY_RETENTION_HOURS` (24) and deleted within the following hour (the OpenAPI text of the header states the configured value). After that the same key starts a new request.
- `POST /auth/password/forgot` commits at the same point for every address, so a parallel request with the same key gets the same answer (409 `idempotency_key_in_use`) whether the address is registered or not.
- `Idempotent-Replayed` is exposed to the allowed CORS origins.
- A replay of `PUT /users/me/password` needs a valid access token: the change ends the old one, so the client refreshes and then retries with the same key.

## OpenAPI texts

Every module route declares a `summary` (imperative, at most 60 characters), a `status_code`, a `response_model` or return annotation, and the error `responses` it can produce through `problem_responses(...)`, which lists the possible `code` values. The route docstring becomes the description in Swagger, so it is written for API consumers: what the route does, who may call it, side effects and errors. `tests/unit/core/test_openapi_contract.py` checks that every module path is under the prefix, that every module route declares problem-detail errors, that body-less responses have no content, and that the whole document equals the snapshot.

After an intended contract change, regenerate the snapshot and review the diff:

```bash
UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/core/test_openapi_contract.py
```
