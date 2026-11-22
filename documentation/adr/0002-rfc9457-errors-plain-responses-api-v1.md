---
status: accepted
date: 2026-11-16
decision-makers: project owner
---

# RFC 9457 errors, plain responses, `/api/v1`

Resolves open decision **D1** (error response format) in the README.

## Context and Problem Statement

Every response used a `success/data/error/metadata` envelope. The envelope had three costs:

* It forced `request` and `start_time` plumbing through every route and service so that `metadata` could be filled.
* `metadata.request_id` could differ from the `X-Request-ID` header.
* Each route wrapped its body in its own `except`, which hid errors and returned tuples such as `(None, "message")` from services.

Which response and error format does the API use?

## Decision Drivers

* Use a standard that clients and tools already understand.
* Remove plumbing from routes and services.
* One central place that maps errors to responses.

## Considered Options

* Plain typed responses for success and RFC 9457 `application/problem+json` for errors.
* Keep the envelope, but build it automatically (middleware or a custom route class).
* Keep the envelope as it is.

## Decision Outcome

Chosen option: "plain typed responses and RFC 9457 errors", because it removes the plumbing instead of hiding it.

* Routes return typed Pydantic models directly. There is no envelope.
* Errors are `application/problem+json` with `type` (`about:blank` or a docs URL), `title`, `status`, `detail`, a stable machine-readable `code` and `request_id`. A 422 adds `errors[]` with `field`, `code` and `message`.
* Services raise `AppError` subclasses: `BadRequest`, `Unauthorized` (with `WWW-Authenticate`), `Forbidden`, `NotFound`, `Conflict`. Module errors subclass them with their own `code`. One handler in the core maps them to responses.
* No `(value, err)` tuple returns, no `try/except Exception` in routes.
* The API is versioned under `/api/v1`. `/health`, `/health/ready` and `/metrics` stay unprefixed.

### Consequences

* Good, because routes and services lose the `request`/`start_time` parameters.
* Good, because `request_id` in the error body matches the `X-Request-ID` header.
* Good, because the `code` field gives clients a stable key that does not depend on message text.
* Good, because the OpenAPI document describes the real success payload.
* Bad, because this is a **breaking change for the frontend**: its API client, types and MSW handlers expect the envelope. The frontend migrates right after S1.
* Bad, because every existing test that asserts the envelope changes.

## Pros and Cons of the Options

### Envelope kept, built automatically

* Good, because clients would not change.
* Bad, because the envelope would stay a custom format that needs documenting, and the `request_id` mismatch and the hidden errors would need their own fixes.

### Envelope as is

* Good, because there is no migration.
* Bad, because it keeps all three costs named above.

## More Information

* RFC 9457, Problem Details for HTTP APIs.
* Conventions: [AGENTS.md → Errors and responses](../../AGENTS.md#errors-and-responses).
* Implemented in S1 of the 2026-11 cleanup.
