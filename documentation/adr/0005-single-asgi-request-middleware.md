---
status: accepted
date: 2026-11-16
decision-makers: project owner
---

# One ASGI request middleware

## Context and Problem Statement

Request handling was spread over four `BaseHTTPMiddleware` classes. Problems:

* The access log did not carry `request_id`.
* 500 responses lacked the request id and the security headers.
* The client IP came from `X-Forwarded-For` and was trusted blindly.
* The metrics middleware derived the route template by walking the route tree. That logic was built to support a router mounted under several prefixes, a case that does not exist.

How is per-request cross-cutting behaviour implemented?

## Decision Drivers

* Every response, including a 500, gets the request id and the security headers.
* Correct client IP behind a proxy, without trusting client-supplied headers.
* Less code, with no machinery for hypothetical cases.

## Considered Options

* One pure-ASGI middleware that does all four jobs.
* Clean up the four separate `BaseHTTPMiddleware` classes.

## Decision Outcome

Chosen option: "one pure-ASGI middleware", because only a single outermost layer sees every response, including the 500.

* One pure-ASGI request middleware handles:
  * the request id: validated or generated, bound to contextvars and echoed in `X-Request-ID`;
  * the access log;
  * HTTP metrics with the labels `method`, `route` and `status`;
  * the security headers.
* It also applies to 500 responses.
* `X-XSS-Protection` is not sent.
* The client IP comes only from the ASGI scope. uvicorn's proxy headers are enabled with a trusted `FORWARDED_ALLOW_IPS`.
* CORS uses Starlette's `CORSMiddleware` with `CORS_ORIGINS` from `Settings`.

### Consequences

* Good, because the access log, metrics and every response share one `request_id`.
* Good, because 500 responses carry the same headers as all others.
* Good, because a spoofed `X-Forwarded-For` cannot set the client IP.
* Good, because `route` is the matched route template, a bounded label.
* Bad, because pure ASGI is lower level than `BaseHTTPMiddleware` and needs care with streaming responses and `send` wrapping.
* Bad, because deployments must set `FORWARDED_ALLOW_IPS` to the proxy addresses, otherwise the client IP is the proxy's.

## Pros and Cons of the Options

### Clean up the separate classes

* Good, because each class keeps one job.
* Bad, because four layers would still run on every request, and the fixes (request id in the access log, headers on 500 responses) would be spread over four classes.

## More Information

* Observability rules: [AGENTS.md → Security rules](../../AGENTS.md#security-rules).
* Implemented in S1 of the 2026-11 cleanup.
