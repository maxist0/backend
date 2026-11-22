# Authentication

How users prove their identity: registration, login, sessions, tokens, locks, emails and the admin actions. Form: explanation with reference tables. The routes are listed in [api.md](api.md#module-routes); the table definitions are in [database.md](database.md#schema-users-and-auth).

The code is in two built-in modules ([ADR 0008](adr/0008-module-layout-and-users-auth-split.md)):

| Module | Owns |
|---|---|
| `src/app/modules/users/` | the `User` model, the profile routes, `CurrentUser` and `OptionalUser` |
| `src/app/modules/auth/` | credentials, sessions, email tokens, the audit log, the flows below, the admin views and the mail tasks |

Token encoding, Argon2 hashing and token hashing are core infrastructure in `src/app/core/security.py`, so `users` can resolve the current user without importing `auth`.

## Tokens

| Token | Format | Lifetime | Stored |
|---|---|---|---|
| Access token | JWT, HS256 pinned in code. Claims `exp`, `iat`, `nbf`, `sub` (user UUID), `jti`, `type=access`, `sid` (session family id), `username` | `ACCESS_TOKEN_EXPIRE_MINUTES` (15) | not stored |
| Refresh token | opaque, 32 random bytes (urlsafe) | `REFRESH_TOKEN_EXPIRE_DAYS` (30) | SHA-256 hash in `sessions`, Postgres only; `HttpOnly` cookie `refresh_token`, `SameSite=Lax`, path `<API_PREFIX>/auth` |
| Email token | opaque, random (`token_urlsafe(32)`) | password reset 1 hour; verification and email change 24 hours | SHA-256 hash in `email_tokens` |

Decoding reads the algorithm from a fixed list, never from the token. It requires `exp`, `iat`, `nbf`, `sub` and `jti`, checks `type=access` and requires `sub` (and `sid`, when present) to be a UUID; `sid` is optional so that tokens issued before it existed stay valid. Access tokens are stateless and stay valid until they expire, with one exception: an admin-locked user gets 401 at once. Revoking a session therefore ends refresh, not access tokens already issued.

## Flows

```
register ──▶ user + argon2id credential + VERIFY_EMAIL token ──▶ mail task
login    ──▶ lock check, admission limiter (PASSWORD_HASH_CONCURRENCY), lock re-check,
             verify password (Argon2 in a thread pool; dummy verify for unknown or inactive users),
             success = conditional atomic UPDATE (refused once locked)
             ──▶ access token (15 min)
                 └▶ refresh token: sha256 → sessions (family_id, device, ip), Postgres only,
                                   cookie refresh_token (HttpOnly, SameSite=Lax, Path=<API_PREFIX>/auth)
refresh  ──▶ atomic UPDATE ... RETURNING on the session row (not revoked, not expired)
         ├─ success: a new pair in the same family
         ├─ token already rotated or revoked: reuse detected, the whole family is revoked
         │   (strict: two concurrent refreshes of the same token count as reuse)
         └─ user inactive or admin-locked: refresh denied, family revoked
            (a timed lock does not affect refresh)
logout       ──▶ revoke the session if it is active (an already rotated token is reuse:
                 family revoked, 401, cookie cleared)
logout-all   ──▶ revoke every session of the user, clear the cookie (bearer required)
password reset  ──▶ revoke all sessions, invalidate every open email token
password change ──▶ revoke all sessions except the caller's (`sid`), invalidate every open email token
```

A refresh that fails with 401 clears the refresh cookie (known issue 25).

**Login answers.** A wrong password, an unknown user and a locked or inactive account all get the same 401 `invalid_credentials`. A locked account waits a calibrated time instead of running Argon2, so the response time does not reveal the lock (known issue 17 for the remaining gap). `POST /auth/password/forgot` answers 202 for every address, also when enqueueing the mail fails (known issue 23 for the timing gap).

**Duplicate registrations.** Two parallel registrations with the same username or email are decided by the unique constraints. The loser gets 409 `user_exists`, never 500.

**Pending email tokens.** Every event that should end an intruder's hold on the account (password change, password reset, admin lock, admin deactivation) invalidates all open email tokens of the user, in the same commit as the session revocation. `POST /auth/email/verify` refuses a token of an inactive or admin-locked account with the same 400 `invalid_token` as an unknown one, and spends it, so lifting the block later does not revive a link that may be in the wrong hands.

**Password confirmation counts like a login.** A wrong current password on `PUT /users/me/password` or `PUT /users/me/email` increments the same failure counter (`LOGIN_MAX_ATTEMPTS`) and sets the same timed lock as failed logins, with `LOGIN_FAILED` and `ACCOUNT_LOCKED` audit rows (detail "Password confirmation"). As after a login lockout, sessions stay valid; while the lock lasts, both endpoints answer 400 `wrong_password` even for the correct password, and the password login answers 401. A correct password resets the counter. An expired timed lock counts as no lock, so wrong confirmations after a lock ran out start a new series.

## Two kinds of lock

| | Timed lock | Admin lock |
|---|---|---|
| Set by | `LOGIN_MAX_ATTEMPTS` (5) consecutive failed logins | the admin action "Lock selected" |
| `users` columns | `is_locked` set, `locked_until` set (`LOGIN_LOCKOUT_SECONDS`, 900 s) | `is_locked` set, `locked_until` NULL (indefinite) |
| Password login | blocked | blocked |
| Existing sessions, access tokens, refresh | stay valid | access tokens get 401 at once; refresh is denied and the family revoked |
| Sessions | untouched | all revoked by the admin action |
| Ends | on its own when `locked_until` has passed | an admin unlocks |

A timed lock blocks only password login. Anyone who knows a username could otherwise trigger it and log that user out, so it must not end sessions. Source: `User.is_admin_locked()` and `User.is_lock_active()` in `src/app/modules/users/models.py`, used by `CurrentUser` in `src/app/modules/users/dependencies.py` and by the refresh gate in `src/app/modules/auth/service.py`.

**Login burst protection.** Each process admits at most `PASSWORD_HASH_CONCURRENCY` password evaluations at a time. A login re-reads the lock state after it is admitted, so guesses queued behind the one that locked the account are refused without an Argon2 evaluation. A successful login is one conditional atomic `UPDATE`: no login succeeds once a lock is committed. With N processes the bound is N × `PASSWORD_HASH_CONCURRENCY`. Rate limiting (D4) is the main control and is still open.

## Serialisation

Refresh and the bulk revocations (`revoke_user_sessions`: logout-all, password reset and change, admin lock and deactivation; `revoke_family`: reuse detection, denied refresh) first take a row lock on the user (`SELECT ... FOR NO KEY UPDATE` on `users`, `lock_user` in `src/app/modules/auth/crud.py`). They queue per user, so a revocation always sees the successor session of a parallel refresh. The global lock order is `email_tokens` → `users` → `sessions`.

## Emails

All mails are sent by tasks in the worker (`src/app/modules/auth/tasks.py`); see [redis.md](redis.md) for the delivery guarantees.

**Token emails** (verification, password reset, email change; [ADR 0010](adr/0010-token-emails-rekeyed-in-the-worker.md)). The request writes the token row with the hash of a value that is never sent and enqueues only the token's id, after the commit. The worker locks that token, gives it a fresh value if it is still open, commits and mails an absolute link: `FRONTEND_URL` plus `FRONTEND_VERIFY_EMAIL_PATH` or `FRONTEND_RESET_PASSWORD_PATH`, plus `?token=…`. Every run (retry, redelivery) sets a new value, so only the latest mail's link works; the expiry stays as the request set it, so a late retry finds the token closed and sends nothing. A newer request closes the older tokens of its purpose, and redeeming a token closes every other open token of the same user and purpose. No token, address or username is stored in Redis.

The worker sends nothing for an inactive or admin-locked account or an already verified address (and invalidates that token), and logs `email.not_sent` with a `reason` (`no_open_token`, `account_blocked`, `already_verified`, `mail_not_configured`). A sent mail logs `email.sent`. The audit rows `email_verification_sent` and `password_reset_requested` record the request; the worker's log records whether a mail went out. A failure to enqueue is logged as `auth.email_enqueue_failed` and answered like a success.

**Security notices.**

- The request that sets a timed lock (login or password confirmation) enqueues a mail to the account's address: password sign-in is locked until `HH:MM UTC` (rounded up to the minute, with the date when the lock ends on another UTC day), and what to do if it was not the user. The login answer stays the uniform 401.
- The worker sends nothing when the lock has ended or became an admin lock, when the account is inactive or its address was never verified, or when a lock notice went out to this user in the last 24 hours (`email.not_sent`, `reason` `not_locked`, `admin_locked`, `account_inactive`, `unverified_address`, `recently_notified`). A sent notice writes the audit row `account_locked_notice_sent`, and only those rows count for the 24 hours. An admin lock sends no mail.
- An email change notifies the old address twice: when it is requested and when it is confirmed (to `email_tokens.previous_email`, stored in the redemption's commit). These mails carry no token, only a link to `FRONTEND_URL`, and are skipped for an inactive account.
- Notices go only to verified addresses: an address nobody confirmed may belong to someone else, so `previous_email` is stored only when the old address was verified.
- A redelivery can send a notice twice; the mails carry no token, so a duplicate is harmless. The 24-hour throttle is check, send, record, and not atomic (known issue 27).

**Cleanup.** `auth.cleanup` runs daily at 03:00 UTC. It deletes sessions and email tokens whose latest timestamp (`expires_at`, and `revoked_at`, `used_at` or `invalidated_at` where set) lies more than `AUTH_CLEANUP_RETENTION_DAYS` in the past. A revoked session stays until its token has also expired for that long; reuse detection ends then (known issue 26). Deleting a token also deletes its `new_email` and `previous_email`. The task deletes in batches of 1000 rows, one transaction each, at most 100 batches per table and run (the log says `capped=true`; the next night continues), skips rows another transaction holds (`FOR UPDATE SKIP LOCKED`) and takes no user lock, so it never waits on a request. It logs `auth.cleanup_done` with `sessions_deleted` and `email_tokens_deleted`. The audit log is not touched.

## Admin panel

The sqladmin panel at `/admin` is mounted when `ADMIN_ENABLED` is true (unset means: only in `dev`). Its views (users, sessions, audit log) are read-only; sqladmin answers create, edit and delete with 403. Changes are actions that call auth service functions (`admin_*` in `src/app/modules/auth/service.py`, wired in `src/app/modules/auth/admin.py`):

| Action | Effect |
|---|---|
| Lock selected | admin lock (indefinite); ends all sessions |
| Unlock selected | lifts the lock and resets failed logins |
| Deactivate selected | ends all sessions |
| Reactivate selected | the reverse |
| Force password change | sets `must_change`; not enforced yet (known issue 16) |
| Revoke selected (sessions) | revokes the selected sessions |

Each user or session is one transaction in the lock order above, and each writes an audit row whose `detail` names the admin (`... by admin <ADMIN_USERNAME>`). A row that no longer exists is skipped. There is no create or delete; account deletion is a later GDPR flow. CSRF protection: the session cookie is `SameSite=Strict`, `HttpOnly` and `Secure` outside `dev`/`unittest` (8 hours), and a Fetch Metadata guard answers 403 to every state-changing admin request without `Sec-Fetch-Site: same-origin` (`src/app/core/admin.py`, [ADR 0006](adr/0006-slim-admin-via-services.md)).

## Security posture

Implemented:

- Argon2id with parameters from `Settings`, a thread pool with a concurrency limiter, rehash on login, and a dummy verify for unknown and inactive users.
- Account lockout after 5 failed logins for 15 minutes with an atomic counter, audit-logged, with a mail to the user (at most one a day). The admin lock is indefinite.
- An atomic refresh gate in Postgres; reuse of a rotated token revokes the family; refresh and bulk revocations are serialised per user.
- Session revocation on password reset (all) and password change (all except the caller's); `POST /auth/logout-all`.
- Tokens (refresh, email) are stored only as SHA-256 hashes; email tokens are atomic and single use.
- `EmailStr`-based email validation that rejects whitespace and control characters; `User-Agent` truncated to 512 and `X-Device-*` headers to 255 characters.
- Error logs carry `error_type` and, for database errors, `db_error_type`, `sqlstate` and `constraint`, never the exception text (`log_unexpected_error` in `src/app/core/errors.py`). SQLAlchemy runs with `hide_parameters`; the database password is masked in logs.
- Security headers on every response, the 500 included; `Cache-Control: no-store` under `API_PREFIX`; a validated request id; the client IP only from the ASGI scope, with proxy headers trusted only from `FORWARDED_ALLOW_IPS`.
- CORS origins from `CORS_ORIGINS`; `*` is refused because credentials are allowed.
- A startup guard outside `dev` and `unittest` ([README → Configuration](../README.md#configuration)).
- OpenAPI docs are off in `prod`. The container runs as a non-root user.

Not yet implemented (see the [Roadmap](../README.md#roadmap)):

- `Secure` flag and `__Secure-` prefix on the refresh cookie (known issue 8).
- Rate limiting per IP and per account, a request body size limit, a request timeout (D4).
- JWT `iss` and `aud` claims, and removing the `username` claim (personal data inside the token).
- HSTS (owned by the TLS-terminating proxy) and CSP `frame-ancestors 'none'`.
- Authentication on `/metrics`.
- Hashing or dropping client IPs in logs (IPs are still logged).
- Dependency and image scanning in CI, an SBOM, signed images.
