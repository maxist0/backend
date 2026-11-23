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
| Access token | JWT, HS256 pinned in code. Claims `exp`, `iat`, `nbf`, `sub` (user UUID), `jti`, `type=access`, `sid` (session family id), `username` | `ACCESS_TOKEN_EXPIRE_MINUTES` (15), never past the end of its session family; `expires_in` in the response says the actual value | not stored |
| Refresh token | opaque, 32 random bytes (urlsafe) | `REFRESH_TOKEN_EXPIRE_DAYS` (30), never past the end of its family: `SESSION_MAX_LIFETIME_DAYS` (90) after the login | SHA-256 hash in `sessions`, Postgres only; `HttpOnly` cookie `refresh_token`, `SameSite=Lax`, path `<API_PREFIX>/auth` |
| Email token | opaque, random (`token_urlsafe(32)`) | password reset 1 hour; verification and email change 24 hours | SHA-256 hash in `email_tokens` |

**Absolute session lifetime.** A login starts a session family that ends `SESSION_MAX_LIFETIME_DAYS` later (`sessions.family_expires_at`). Every rotation copies that end unchanged and gives the new session `min(now + REFRESH_TOKEN_EXPIRE_DAYS, family end)`; a CHECK constraint makes sure no session expires after its family. An access token issued shortly before the family end expires with the family. A refresh after the family end is refused like any expired token (401 `invalid_refresh_token`), and the user must log in again. Active use therefore keeps a login alive for at most `SESSION_MAX_LIFETIME_DAYS` ([OWASP Session Management Cheat Sheet, absolute timeout](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)). The setting must be at least `REFRESH_TOKEN_EXPIRE_DAYS`; it applies to logins made after a change.

Decoding reads the algorithm from a fixed list, never from the token. It requires `exp`, `iat`, `nbf`, `sub` and `jti`, checks `type=access` and requires `sub` (and `sid`, when present) to be a UUID; `sid` is optional so that tokens issued before it existed stay valid. Access tokens are stateless and stay valid until they expire, with two exceptions: an admin-locked user gets 401 at once, and so does every token issued before the account's last password change or reset. Revoking a session alone therefore ends refresh, not access tokens already issued.

**Access tokens after a password change.** `CurrentUser` (and `OptionalUser`) compares the token's `iat` with `users.password_changed_at` and refuses an older token with 401 `unauthorized` and `WWW-Authenticate: Bearer`, the answer of any invalid token; the log gets `auth.access_token_outdated` with the `user_id`. After a reset no session is left, so the user signs in again. After `PUT /users/me/password` the caller's own access token is refused too; its session is kept, so the client refreshes once and goes on with the new token. `iat` has whole seconds, so the change time is cut to whole seconds before the comparison (`iat < floor(password_changed_at)`): a token issued in the same second as the change is kept, even if it was issued up to one second before the change. Both values come from the application clock (`utcnow()` writes `password_changed_at`), so database clock skew does not matter; the API hosts' clocks must agree to within that second. The users module reads `iat` from `TokenData.issued_at`, which the core decoder (`decode_token` in `src/app/core/security.py`) fills after PyJWT has validated the claim: parsing the JWT again in a module would duplicate the core's token handling, so the core got this one additive field (owner decision, 2026-11-22, no ADR).

## Flows

```
register ──▶ user + argon2id credential + VERIFY_EMAIL token ──▶ mail task
login    ──▶ rate limits (per address; per address and username), then
             username lookup ignoring case (a name outside the username pattern counts as unknown), lock check, admission limiter (PASSWORD_HASH_CONCURRENCY), lock re-check,
             verify password (Argon2 in a thread pool; dummy verify for unknown or inactive users),
             success = conditional atomic UPDATE (refused once locked)
             ──▶ access token (15 min)
                 └▶ refresh token: sha256 → sessions (family_id, family end, device, ip), Postgres only,
                                   cookie refresh_token (HttpOnly, SameSite=Lax, Path=<API_PREFIX>/auth)
refresh  ──▶ atomic UPDATE ... RETURNING on the session row (not revoked, not expired)
         ├─ success: a new pair in the same family, never expiring after the family end
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

**Login answers.** A wrong password, an unknown user and a locked or inactive account all get the same 401 `invalid_credentials`. A locked account waits a calibrated time instead of running Argon2, so the response time does not reveal the lock (known issue 17 for the remaining gap). `POST /auth/password/forgot` answers 202 for every address, also when enqueueing the mail fails (known issue 23 for the timing gap). Above a rate limit both answer 429 `rate_limited` (see [Rate limits](#rate-limits)), the same for every username and address.

**Duplicate registrations.** Usernames are unique regardless of case (`Alice` and `alice` are one name) and keep the casing given at registration; login accepts any casing. A username consists of ASCII letters, digits, `-` and `_` only (the whole value; a trailing line break is refused with 422). A login name that does not match that pattern is treated as an unknown user without a database lookup, because PostgreSQL's `lower()` folds some non-ASCII letters onto ASCII ones (the Kelvin sign, U+212A, onto `k`). Two parallel registrations with the same username or email are decided by the unique index `uq_users_lower_username` and the constraint `uq_users_email`. The loser gets 409 `user_exists`, never 500.

**Pending email tokens.** Every event that should end an intruder's hold on the account (password change, password reset, admin lock, admin deactivation) invalidates all open email tokens of the user, in the same commit as the session revocation. `POST /auth/email/verify` refuses a token of an inactive or admin-locked account with the same 400 `invalid_token` as an unknown one, and spends it, so lifting the block later does not revive a link that may be in the wrong hands.

**Email tokens die with a password change.** An email token of any purpose whose `created_at` is not after the owner's `users.password_changed_at` is refused like an expired one (400 `invalid_token`), and the worker does not mail it. This covers a token the change could not invalidate because its request had not committed yet. The condition sits in the atomic redemption (`consume_email_token`), in the reset's pre-check and in the worker's token lock, as a plain sub-select on `users` that takes no lock, so the lock order stays as documented. The token is refused, not marked: the condition can never become true again, and the cleanup deletes it after expiry. `created_at` of an email token is written from the application clock (`utcnow()`), like `password_changed_at`; a tie counts as before the change (fails safe), and a token requested even a millisecond after the change works. The worker's re-keying keeps the row and its `created_at`, so a token requested after a change is not affected by when the mail goes out. An email change also checks the credential version under the user lock before it commits its token: if a reset or password change committed after its password confirmation, it creates no token and answers 400 `wrong_password` (no failed attempt counted, a `login_failed` audit row, log `auth.email_change_credentials_changed`). The two rules together close the race between an email change confirmed with the old password and a reset: either the request sees the reset and creates nothing, or the reset takes the user lock after the token was committed and stamps a later `password_changed_at`.

**Password confirmation counts like a login.** A wrong current password on `PUT /users/me/password` or `PUT /users/me/email` increments the same failure counter (`LOGIN_MAX_ATTEMPTS`) and sets the same timed lock as failed logins, with `login_failed` and `account_locked` audit rows (detail "Password confirmation"). As after a login lockout, sessions stay valid; while the lock lasts, both endpoints answer 400 `wrong_password` even for the correct password, and the password login answers 401. A correct password resets the counter. An expired timed lock counts as no lock, so wrong confirmations after a lock ran out start a new series.

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

**Login burst protection.** Each process admits at most `PASSWORD_HASH_CONCURRENCY` password evaluations at a time. A login re-reads the lock state after it is admitted, so guesses queued behind the one that locked the account are refused without an Argon2 evaluation. A successful login is one conditional atomic `UPDATE`: no login succeeds once a lock is committed. An expired timed lock is lifted by a conditional `UPDATE` as well, so a lock committed by a parallel guess after the check is never erased. With N processes the bound is N × `PASSWORD_HASH_CONCURRENCY`. The rate limits below run in front of all of this and are the main control.

## Rate limits

The auth routes are rate limited in the app on Redis ([ADR 0013](adr/0013-rate-limiting-in-the-app-on-redis.md)); the policies and their values are listed in [api.md](api.md#limits). How they meet the flows above:

- **Login.** Two policies: 20 attempts a minute per client address (route dependency), and 10 per 15 minutes per client address and lower-case submitted username (`authenticate_form_user` in `src/app/modules/auth/dependencies.py`, before `authenticate_user`). Both are counted before the lookup, the lock check, the admission limiter and Argon2, so a refused attempt costs one Redis round trip and no hash, and the count is the same whether the account exists, is locked or not. The account lock still applies to every attempt that gets through: five failures lock the account long before the per-username limit (10) is reached, and the lock mail is unchanged. An IPv6 client is counted per /64; a third policy counts 100 attempts a minute per /48, so a client that rotates through the /64s of its site is bounded too (IPv4 clients are not counted by it).
- **Forgot password.** 5 requests per 15 minutes per client address, and 3 per hour per lower-case email address from any client, counted at the start of `forgot_password` before the account lookup. The second limit caps the reset mails one address can receive and the timing samples one address yields (known issue 23). IPv6 clients are also limited to 25 per 15 minutes per /48.
- **Refresh.** 60 per minute per client address, and 10 per minute per session family, counted in `refresh_session` after the token's family is read and before the user lock. Only a live token (not revoked, not expired) is counted: a rotated token is never refused with 429 but always reaches the reuse check, so whoever keeps the family's window full (a thief) cannot hide the victim's replay ([RFC 9700 section 4.14.2](https://www.rfc-editor.org/rfc/rfc9700#section-4.14.2)). A refused refresh rotates nothing, takes no lock and leaves the cookie valid. An unknown token never reaches the family limit; it gets 401.
- **Reset and verification tokens.** 10 attempts per 15 minutes per client address on each of `POST /auth/password/reset` and `POST /auth/email/verify`. Tokens are 256-bit random values, so the limit guards the database, not the token.
- **Signed-in users.** The verification resend (3 per hour) and the password and email changes (5 per 15 minutes) count per user (`by_user`). A request without a valid token gets 401 and is not counted. The password confirmation still counts towards the account lock. Mails are also limited per recipient, from all accounts together: 3 email-change links an hour to one new address, counted after the password check and before the address is looked up (the 429 does not tell whether it is registered), and 3 verification mails an hour to one address.

- **Admin panel.** The admin login counts 10 attempts per 15 minutes per client address before it compares the credentials; a refused attempt gets sqladmin's normal login failure and logs `admin.login_rate_limited`.

Keys hold no address, username or email in clear text, only an HMAC keyed from `JWT_SECRET_KEY`. When Redis fails the limits let requests through (fail-open), and each process skips Redis for 5 s after an error, so an outage adds about a second to one request per process every 5 s instead of to every request (known issue 31). `rate_limit_fail_open_total` and the log event `rate_limit.fail_open` show it, and the account lock and the admission limiter still work. `RATE_LIMIT_ENABLED=false` outside `dev` and `unittest` logs `rate_limit.disabled` at startup.

## Serialisation

Refresh and the bulk revocations (`revoke_user_sessions`: logout-all, password reset and change, admin lock and deactivation; `revoke_family`: reuse detection, denied refresh) first take a row lock on the user (`SELECT ... FOR NO KEY UPDATE` on `users`, `lock_user` in `src/app/modules/auth/crud.py`). They queue per user, so a revocation always sees the successor session of a parallel refresh. The global lock order is `email_tokens` → `users` → `sessions` → `password_credentials`.

**Password changes end every other session.** A password reset or change writes the new hash, sets `users.password_changed_at` (the credential version), invalidates the open email tokens and revokes the sessions in one transaction; a reset redeems its token in the same transaction. A login reads the hash and `password_changed_at` in one statement before it verifies the password, and its success `UPDATE` requires `password_changed_at` to be unchanged. A password change confirms the current password the same way and writes the new hash only if `password_changed_at` is still the value read with the confirmed hash; if a reset or another change committed in between, nothing is written and the request answers 400 `wrong_password` (no failed attempt counted, a `login_failed` audit row "Password confirmation, password changed meanwhile", log `auth.password_change_credentials_changed`). A login that verified the old password while a reset or change committed is therefore refused with the usual 401 `invalid_credentials` and gets no session. It does not count as a failed attempt (the password was right when it was read); it writes a `login_failed` audit row ("Password changed during login") and logs `auth.login_credentials_changed`. The login's rehash of an outdated Argon2 hash keeps `password_changed_at` (same password), replaces only the hash it verified, so it never overwrites a newer password, and is best effort: a failed rehash logs `auth.password_rehash_failed` and the login still succeeds.

**Transactions.** The crud functions never commit; each service function commits its use case with `await db.commit()`, and mail tasks are enqueued only after that commit. Registration, for example, writes the user, the password credential, the verification token and both audit rows in one commit. A few flows commit earlier on purpose:

- **Before a refusal.** A failed password (login or confirmation), a guess on a locked account, a denied refresh and a reused refresh token commit the counter, the lock, the revocation and the audit row before the 401 or 400 is raised, which would otherwise roll them back.
- **Before Argon2.** No row lock lasts over password hashing. Login commits an expired lock's removal before it checks the password, and commits the successful login before a rehash. Password confirmation commits the counter reset before the caller hashes the new password. A password reset first checks its token with a plain `SELECT` (an invalid token costs no Argon2), ends that read transaction, hashes the new password with no transaction open, and then redeems the token and writes everything else in one transaction; a token redeemed meanwhile gets 400 `invalid_token`, and a failure before the commit leaves the token open and the sessions intact.
- **Inside the limiter.** A failed login commits its counter before it leaves the admission limiter, so the guesses queued behind it see the lock.
- **Batches.** `auth.cleanup` commits after every batch (see Cleanup below).

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

**Cleanup.** `auth.cleanup` runs daily at 03:00 UTC. It deletes email tokens whose latest timestamp (`expires_at`, and `used_at` or `invalidated_at` where set) lies more than `AUTH_CLEANUP_RETENTION_DAYS` in the past, and sessions whose family end (`family_expires_at`, and `revoked_at` where later) lies that far in the past. Every session of a family, a rotated one included, therefore stays until its family has ended: a replayed rotated token is either found and revokes the family, or its family has ended and every token of it is refused anyway. Sessions are kept for up to `SESSION_MAX_LIFETIME_DAYS` + `AUTH_CLEANUP_RETENTION_DAYS` (120 days by default). A rotated session keeps no IP address, user agent or device: the rotation sets them to NULL, so only the active session of a family holds them, and the admin session view shows them empty for rotated rows. Deleting a token also deletes its `new_email` and `previous_email`. The task deletes in batches of 1000 rows, one transaction each, at most 100 batches per table and run (the log says `capped=true`; the next night continues), skips rows another transaction holds (`FOR UPDATE SKIP LOCKED`) and takes no user lock, so it never waits on a request. It logs `auth.cleanup_done` with `sessions_deleted` and `email_tokens_deleted`. The audit log is not touched.

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
- Rate limits per client address, per address and username, per email address, per user and per session family, counted before any lookup or Argon2; keys stored only as HMACs ([Rate limits](#rate-limits)).
- A request body limit (`MAX_REQUEST_BODY_BYTES`, 413).
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
- A request timeout and a coarse per-address limit at the proxy (deploy round).
- JWT `iss` and `aud` claims, and removing the `username` claim (personal data inside the token).
- HSTS (owned by the TLS-terminating proxy) and CSP `frame-ancestors 'none'`.
- Authentication on `/metrics`.
- Hashing or dropping client IPs in logs (IPs are still logged).
- Dependency and image scanning in CI, an SBOM, signed images.
