---
status: accepted
date: 2026-11-17
decision-makers: project owner
---

# Token emails are re-keyed in the worker; tasks get a database session

## Context and Problem Statement

The verification, password-reset and email-change mails were enqueued with the recipient address, the username and the raw token as task arguments. With the Redis stream broker ([ADR 0009](0009-taskiq-redis-stream-broker-and-delayed-retries.md)) those arguments sit in the stream until a worker acknowledges the entry, and a failed attempt stores them again in a retry schedule (`taskiq_retry:*`) that does not expire. Anyone who can read Redis could redeem a link, and a retry hours later would still mail a link that the user had already replaced.

Delivery is at least once, so the mail task must also be idempotent: a retry or a redelivery must not leave two working links.

How can the mail task run without personal data or tokens in Redis, and stay safe to run twice?

## Decision Drivers

* No token, address or username in Redis: not in the stream, the retry schedules or the results.
* A task that runs again (retry, redelivery) leaves exactly one working link.
* A late retry must not mail a link the user no longer expects.
* The request keeps its answer and its lock order (`email_tokens -> users -> sessions`).
* No new dependency; the core change stays small.

## Considered Options

* Re-key in the worker: the request writes the token row with the hash of a value that is never sent; the task gets the token's id, gives that token a fresh value and mails it.
* Create the token in the worker: the request writes no row; the task invalidates older tokens and inserts a new one.
* Keep the raw token in the task arguments and encrypt them.

## Decision Outcome

Chosen option: **re-key in the worker**.

* The request creates the token row as before (older tokens of the purpose are invalidated, the audit row is written, `new_email` is stored for an email change) with the hash of `secrets.token_urlsafe(32)`, a value that is never sent. After the commit it enqueues `send_token_email_task(token_id)`. The token's id is a random UUID: neither personal nor secret.
* The task locks that token (`WHERE id = :id` and still open: not used, not invalidated, not expired), sets the hash of a fresh value, commits, then renders and sends. Owner and purpose come from the row. Every run sets a new value, so only the latest mail's link works. The expiry stays as the request set it (reset 1 h, verification and email change 24 h), so a late retry finds the token closed and sends nothing.
* Targeting one token, not "the newest open token of the user and purpose", means a task never keys a leftover token that parallel requests created. Redeeming a token (`verify_email`, `reset_password`) closes every other open token of the same user and purpose in the same commit.
* The task sends nothing for an inactive or admin-locked account, and no verification mail for a verified address; it invalidates that token. Without a mail configuration it sends nothing and leaves the token untouched.
* The audit rows `email_verification_sent` and `password_reset_requested` record the request, with a "requested" detail; the enum values stay. Whether a mail went out is logged by the worker: `email.sent`, or `email.not_sent` with a `reason`.
* Tasks get a database session through taskiq's dependency injection: `TaskDbSession = Annotated[AsyncSession, TaskiqDepends(get_task_db)]`, exported from `app.core`. A worker process builds the engine at `WORKER_STARTUP` with the API's engine factory and disposes it at `WORKER_SHUTDOWN`. The API and the scheduler fire only the client events and open no pool for tasks.
* Database errors do not leave a task with their text. taskiq logs a failed task's exception with its traceback and stores it as the result before it closes the task's dependencies, so only code inside the task call can replace it. The core `task` decorator therefore wraps the task function (`functools.wraps`, so the signature, the `TaskiqDepends` parameters and the `kiq()` typing stay) and turns `DBAPIError` and raw psycopg errors into `TaskDatabaseError`, whose message holds only the exception types and the SQLSTATE. SQLSTATE class 08 (connection exception), 40001 (serialization failure), 40P01 (deadlock detected), 57P01 (admin shutdown) and connection failures without a SQLSTATE become `TransientTaskDatabaseError` and are retried (PostgreSQL documentation, Appendix A, "PostgreSQL Error Codes").
* A failure to enqueue after the commit is logged (`auth.email_enqueue_failed`) and answered as a success; the user can ask again.

### Consequences

* Good, because Redis holds only a token id for these tasks; the stream, the retry schedules and the results carry no token value, address or username.
* Good, because the mail task is idempotent: a second run replaces the first link of the same token instead of adding one.
* Good, because the stored expiry bounds every retry: a retry that the scheduler sends late, after a scheduler outage for example, sends nothing once the link has expired.
* Good, because a module task that hits a constraint violation cannot write the conflicting value into the worker log or the result store.
* Bad, because a redelivery after a successful send makes the first mail's link stop working; the user has to use the latest mail.
* Bad, because each worker process opens its own connection pool (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`), which counts against PostgreSQL's `max_connections`.
* Bad, because two parallel redemptions of two different open tokens of the same user and purpose lock each other's rows; PostgreSQL resolves the deadlock by aborting one transaction, whose request fails. Two open tokens of one purpose exist only after parallel requests.
* Neutral, because the task's `db` parameter needs a `TaskiqDepends()` default so that `kiq()` type-checks without it; the dependency in the annotation wins.

## Pros and Cons of the Options

### Create the token in the worker

* Good, because the request writes less.
* Bad, because the request could no longer report what it started: the audit row, the pending `new_email` of an email change and the invalidation of older tokens would move into a task that may run late or never, and the request would have to pass the new address through Redis.

### Encrypt the task arguments

* Good, because the request would not change.
* Bad, because it needs a key, key rotation and a decrypting worker, and a retry would still mail an old link.

## Addendum 2026-11-18: security notices, `previous_email` and the cleanup task

The same pattern carries three mails without a token, approved by the owner on 2026-11-17:

* **Account locked.** The request that sets a timed lock enqueues `email.send_account_locked_notice` with the user's id only, after the audit commit; the login answer stays 401 `invalid_credentials`. The worker reads the address and `locked_until`, and sends nothing when the lock has ended, became an admin lock or the account is inactive. At most one notice per user in 24 hours: the worker looks for an `account_locked_notice_sent` audit row of the last 24 hours, which only a sent notice writes. A new enum value was chosen over reusing `account_locked` (that would count each lock twice in the audit trail) and over counting the lock rows (a lock without a sent mail would suppress the next notice, and a sustained attack would keep the user from ever getting a second one). The admin lock sends nothing.
* **Email change requested / completed.** Both notices go to the old address and are enqueued with the email-change token's id. The address the change replaced is not in `users` after the confirmation, so the redemption writes it to the new nullable column `email_tokens.previous_email` in the same commit. The requested notice goes to `previous_email` once the change is confirmed, otherwise to the current address. Both are skipped for an inactive account.
* **Verified addresses only** (decided 2026-11-18 after the security review). An address that was never verified may belong to someone else, who would learn the username and that the account changes. The lock notice and both email-change notices are therefore sent only to verified addresses: `previous_email` is stored only when the old address was verified, and an unverified recipient is logged as `email.not_sent` with `reason=unverified_address`.
* **Retention.** `previous_email` and `new_email` are personal data kept beyond the token's use. The nightly `auth.cleanup` task (03:00 UTC) deletes email tokens, and with them both addresses, `AUTH_CLEANUP_RETENTION_DAYS` (default 30, 1–365) after their latest timestamp, and ended sessions likewise; a revoked session is kept until its token has also expired for that long. Reuse detection therefore ends `REFRESH_TOKEN_EXPIRE_DAYS` + retention after a rotated token was issued: a replay after that is a plain 401 without family revocation (README Known issue 26). The real fix is the absolute session (family) lifetime planned for review Phase 2. A run deletes at most 100 batches of 1,000 rows per table, well within the 600 s redelivery idle time; the next night continues. That bound on `previous_email` and `new_email` is why the owner accepted the new column. The audit log is not touched.

Consequences: a redelivery can send a notice twice; the mails carry no token, so a duplicate is harmless. The throttle is check, send, record and not atomic, so after a worker backlog several lock notices can go out together, at most one per lock (Known issue 27).

The same review found that an expired timed lock was never cleared by a wrong password confirmation, so confirmations counted past the threshold and never locked again. `register_failed_login` now treats an expired timed lock as no lock and starts a new series; the admin lock is never weakened.
