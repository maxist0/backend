# Architecture decision records

Decisions that shape the backend. Each record uses the [MADR 4.0](https://adr.github.io/madr/) format. A decision taken from the README's [Open decisions](../../README.md#open-decisions) table gets an ADR, and the table row links to it.

Naming: `NNNN-kebab-title.md`, numbered in the order the decisions were taken. A record is never rewritten after acceptance. A later decision supersedes it with a new ADR and a status change.

The decisions below were taken by the owner on 2026-11-16. They are implemented in steps S1–S3 of the 2026-11 cleanup (S1 structure, S2 taskiq reliability, S3 style pass, strict ruff/mypy, docs). Until those steps land, the code does not match the records.

| ADR | Title | Status | Date | Open decision |
|---|---|---|---|---|
| [0001](0001-core-and-module-contract.md) | Core and module contract | accepted | 2026-11-16 | |
| [0002](0002-rfc9457-errors-plain-responses-api-v1.md) | RFC 9457 errors, plain responses, `/api/v1` | accepted | 2026-11-16 | D1 |
| [0003](0003-caching-via-cached-decorator.md) | Caching via a `@cached` decorator | accepted | 2026-11-16 | |
| [0004](0004-logging-stdout-only.md) | Logging to stdout only | accepted | 2026-11-16 | |
| [0005](0005-single-asgi-request-middleware.md) | One ASGI request middleware | accepted | 2026-11-16 | |
| [0006](0006-slim-admin-via-services.md) | Slim admin that calls services | accepted | 2026-11-16 | |
| [0007](0007-test-split-unit-integration.md) | Test split: unit and integration | accepted | 2026-11-16 | |

Conventions that follow from these decisions are in [AGENTS.md](../../AGENTS.md).
