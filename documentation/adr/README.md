# Architecture decision records

Decisions that shape the backend. Each record uses the [MADR 4.0](https://adr.github.io/madr/) format. A decision taken from the README's [Open decisions](../../README.md#open-decisions) table gets an ADR, and the table row links to it.

Naming: `NNNN-kebab-title.md`, numbered in the order the decisions were taken. A record is never rewritten after acceptance. A later decision supersedes it with a new ADR and a status change.

The first eight decisions were taken by the owner on 2026-11-16, the next two on 2026-11-17, ADR 0011 (open decision D6) was recorded on 2026-11-22, and ADR 0012 (open decision D10) the same day. ADRs 0001 to 0011 are implemented in steps S1–S3 of the 2026-11 cleanup (S1 structure, S2 taskiq reliability, S3 style pass, strict ruff/mypy, docs); ADR 0012 in step B1.1 of review phase 2 (data layer). ADR 0013 (open decision D4) and ADR 0014 (open decision D9) were decided and implemented on 2026-11-23.

| ADR | Title | Status | Date | Open decision |
|---|---|---|---|---|
| [0001](0001-core-and-module-contract.md) | Core and module contract | accepted, amended by 0008 | 2026-11-16 | |
| [0002](0002-rfc9457-errors-plain-responses-api-v1.md) | RFC 9457 errors, plain responses, `/api/v1` | accepted | 2026-11-16 | D1 |
| [0003](0003-caching-via-cached-decorator.md) | Caching via a `@cached` decorator | accepted | 2026-11-16 | |
| [0004](0004-logging-stdout-only.md) | Logging to stdout only | accepted | 2026-11-16 | |
| [0005](0005-single-asgi-request-middleware.md) | One ASGI request middleware | accepted | 2026-11-16 | |
| [0006](0006-slim-admin-via-services.md) | Slim admin that calls services | accepted | 2026-11-16 | |
| [0007](0007-test-split-unit-integration.md) | Test split: unit and integration | accepted | 2026-11-16 | |
| [0008](0008-module-layout-and-users-auth-split.md) | Fixed module layout, users separate from auth, core consolidation | accepted | 2026-11-16 | |
| [0009](0009-taskiq-redis-stream-broker-and-delayed-retries.md) | Taskiq on a Redis stream with acknowledgements and delayed retries | accepted | 2026-11-17 | |
| [0010](0010-token-emails-rekeyed-in-the-worker.md) | Token emails are re-keyed in the worker; tasks get a database session | accepted | 2026-11-17 | |
| [0011](0011-type-checking-mypy-and-pyright-strict.md) | Type checking: mypy and pyright strict are gates, ty is informational | accepted | 2026-11-22 | D6 |
| [0012](0012-schema-conventions-uuidv7-value-enums-usernames.md) | Schema conventions: UUIDv7 keys, model mixins, constraint names, checked VARCHAR enums, case-insensitive usernames | accepted | 2026-11-22 | D10 |
| [0013](0013-rate-limiting-in-the-app-on-redis.md) | Rate limiting in the app on Redis, and a request body limit | accepted | 2026-11-23 | D4 |
| [0014](0014-idempotency-keys-in-postgres-same-transaction.md) | Idempotency keys in PostgreSQL, in the transaction of the business write | accepted | 2026-11-23 | D9 |

Conventions that follow from these decisions are in [AGENTS.md](../../AGENTS.md).
