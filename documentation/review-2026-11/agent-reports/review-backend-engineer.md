# Backend review: backend-engineer (logic, assembly, modern FastAPI, extensibility)

Date: 2026-11-16. Scope: `backend/src/api/auth/*`, `src/api/user/*`, `src/utils/*`, `src/main.py`, `src/common/*`, `src/admin.py`, `app.py`. Read-only; no repository file changed.

## 1. Outcome

The auth flows have **one critical and several high-severity logic bugs** that README does not list. The worst is that refresh-token family revocation (reuse detection, admin revocation) does not work, because the Redis fast path never consults Postgres. App assembly has a structural defect: the settings global is overwritten at import time, and `import src.main` crashes without `app.py`. That defect blocks the planned `fastapi run`, an app factory and settings-as-dependency.

Counts: **1 critical, 6 high, 16 medium, 9 low** (32 findings).

## 2. Changes

None in the repository. This report and two probe scripts were written to the scratchpad (`settings_probe.py`, `contract_probe.py`; in-memory only, they register a temporary `/__boom` route on the imported app object).

## 3. Verification performed

| Command | Result |
|---|---|
| `uv run --frozen --no-sync python -c "print('ok')"` | ok; `.venv` is CPython 3.14.4. **Known issue 14 is not reproducible.** |
| `.venv/Scripts/python.exe -c "import src.main"` | `AttributeError: 'NoneType' object has no attribute 'DATABASE_URL'` (database.py:37) |
| `settings_probe.py` (init_settings("unittest"), then import src.main) | `config.settings` is **DEV** afterwards; `src.admin.settings` is DEV, while `main`/`auth.service`/`database`/`email_tasks` hold UNITTEST: two Settings objects in one process |
| `contract_probe.py` (TestClient without lifespan) | 404/405/missing-bearer return `{"detail":...}`; bad bearer 401 without `WWW-Authenticate`; unhandled 500 without CORS/`X-Request-ID`/security headers; body `metadata.request_id` differs from the `X-Request-ID` header; timestamps have no offset; `GET /` returns 500 when cashews is not set up; every auth/user route has an empty 200 schema in OpenAPI |
| Argon2 micro-benchmark (`PasswordHasher()` defaults t=3, m=64 MiB, p=4) | verify takes **41 ms** on average; an inline verify stalls the event loop for up to **121 ms**; with `asyncio.to_thread` the stall is 14 ms (Windows timer resolution) |
| `uv run --frozen --no-sync mypy src` | **6 errors** (admin.py:90,107,126,145,243,291, `UploadFile | str` passed to `uuid.UUID`). AGENTS.md DoD says this must pass. |
| `uvx ruff@0.15.0 check src tests` | passes |
| `uvx ruff@0.15.0 format --check src tests` | fails on `src/admin.py` (Known issue 13 confirmed) |

Not run: `pytest`. It writes `.coverage`/`htmlcov` into the repo (addopts `--cov`) and needs Docker. The race conditions (BE-02, BE-08, BE-09) and the revocation bypass (BE-01) are **verified by code trace, not by execution**. Each one has a reproducible test sketch in its fix proposal.

Installed versions, from `uv.lock`: fastapi 0.136.1, starlette 0.52.1, pydantic 2.13.3, pydantic-settings 2.14.0, sqlalchemy 2.0.49, uvicorn 0.40.0, cashews 7.5.0, taskiq 0.12.2, taskiq-redis 1.2.2, redis 7.4.0, argon2-cffi 25.1.0, pyjwt 2.11.0, sqladmin 0.25.0, fastapi-mail 1.6.2, anyio 4.12.1. `fastapi-cli` is **not installed**.

Latest FastAPI on 2026-11-16 is **0.163.1** (2026-11-15), per the GitHub releases page and fastapi.tiangolo.com/release-notes. The agent brief said 0.162.5.

---

## 4. Findings

Format of each finding: ID, severity, category, location, evidence, impact, fix, effort, breaking/frontend impact, README reference.

### BE-01: Refresh-token family revocation is bypassed by the Redis fast path
- **Severity:** critical
- **Category:** bug
- **Location:** `src/api/auth/service.py:371-392`, `src/api/auth/crud.py:151-171`, `src/admin.py:289-303`
- **Evidence (trace):**
  1. Login issues A, stored in DB A and Redis `rt:A`.
  2. The attacker steals A. The victim refreshes: Redis A is deleted, DB A is revoked, B is created (DB B, Redis `rt:B`).
  3. The attacker presents A: Redis miss, DB A is revoked, so `revoke_family()` runs. That function only runs `UPDATE sessions SET is_revoked=true` (crud.py:163-170) and **never deletes `rt:B`**.
  4. Whoever holds B refreshes: `get_cached_refresh_token` hits (service.py:371). `revoke_session` updates an already-revoked row, then `create_session_tokens` issues C in the same family. The family lives on.

  The fast path never checks `sess.is_revoked` or `expires_at`. Admin "Revoke selected" (admin.py:289) and any future "logout-all" or password-reset revocation (Known issue 5) are bypassed the same way. The test `test_old_refresh_token_triggers_reuse_detection` (tests/auth/test_auth.py:267-284) does not catch this: after `client.cookies.set("refresh_token", old_refresh)` the jar sends the old token again for `r2`, so the newest token is never presented on its own.
- **Impact:** RFC 9700 §4.14.2 (refresh-token rotation and reuse detection) is defeated, and every server-side session revocation is ineffective for up to `REFRESH_TOKEN_EXPIRE_DAYS` (30). A stolen token family stays usable after detection.
- **Fix:** Make Postgres the only authority for refresh. Atomically consume with `UPDATE sessions SET is_revoked=true, revoke_reason='rotated', revoked_at=now() WHERE token_hash=:h AND NOT is_revoked AND expires_at>now() RETURNING id, user_id, family_id`. 0 rows means: look up the row; if it is revoked, apply reuse handling (with the frontend's reuse interval, see BE-02); otherwise return 401.

  Drop the Redis mirror for refresh tokens. Refresh load is about `active_clients / 900 s` (10 k clients is about 11 req/s), which is one indexed UPDATE on the unique `token_hash`. If a cache is still wanted, key it by `hash_token(raw)` and delete keys on every revocation (family, user, admin).

  Add a regression test: login, refresh (B), present A (reuse), then present **only B**, expecting 401.
- **Effort:** M
- **Breaking:** no; frontend: none.
- **README:** NEW. "Session store: Postgres as source of truth, Redis as fast path" in Design decisions is not what the code does.

### BE-02: Concurrent refresh with the same token forks the family (non-atomic rotation)
- **Severity:** high
- **Category:** bug
- **Location:** `service.py:371-392`
- **Evidence:** `get` (crud.py:285) followed by a separate `delete` (crud.py:295). Two requests that both pass the GET each create a new session in the same family. No reuse is detected and two valid refresh tokens exist.
- **Impact:** An attacker racing the victim gets an undetected parallel session. On the legitimate side, this is exactly the case the frontend solved with Web Locks, but other devices or clients are not protected.
- **Fix:** The atomic conditional UPDATE from BE-01 (single winner). If Redis is kept, use `GETDEL` (Redis ≥ 6.2). Implement **frontend backend-task 5** (reuse interval) in the same change: when 0 rows are updated and the row has `revoke_reason='rotated'`, `revoked_at > now() - REFRESH_TOKEN_REUSE_INTERVAL_SECONDS` and it is the newest superseded token of the family, issue a new pair and write an audit event. Otherwise revoke the family. This needs a `replaced_by`/`rotated_to` column (coordinate with db-engineer).
- **Effort:** M
- **Breaking:** no; frontend: fixes "lost refresh response logs out every device" (task 5).
- **README:** NEW. Frontend task 5 is not in the backend Roadmap.

### BE-03: Argon2 hash/verify runs on the event loop
- **Severity:** high
- **Category:** bug (async correctness)
- **Location:** `service.py:52-62` (`_ph.hash`/`_ph.verify`), called at 175, 265, 500, 590, 593
- **Evidence:** Measured 41 ms per verify and up to 121 ms loop stall. Change-password does verify plus hash (about 80 ms blocking).
- **Impact:** Each login blocks every other request in that worker. About 25 logins/s saturate a process. It is also a cheap DoS amplifier against `/auth/login` (no rate limit yet, D4).
- **Fix:**
  ```python
  _HASH_LIMITER = anyio.CapacityLimiter(settings.PASSWORD_HASH_CONCURRENCY)  # memory bound: N × 64 MiB
  async def verify_password(pw: str, h: str) -> bool:
      return await anyio.to_thread.run_sync(_verify_sync, pw, h, limiter=_HASH_LIMITER)
  ```
  argon2-cffi calls through CFFI, which releases the GIL (inferred from CFFI API-mode semantics, consistent with the measured 14 ms). Use a dedicated limiter, not the default 40-token thread pool: 40 × 64 MiB = 2.5 GiB peak. FastAPI 0.163.0 adds `Depends(max_concurrency=...)` (GitHub release notes, 2026-11-14), but its overflow semantics are not documented yet, so keep `CapacityLimiter`.

  Also call `check_needs_rehash()` after a successful verify and upgrade the stored hash.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW. AGENTS.md golden rule 1 says "async only", but CPU-bound work is not covered there.

### BE-04: The settings global is overwritten at import time, and the app cannot be imported without `app.py`
- **Severity:** high
- **Category:** bug / modernisation
- **Location:** `src/common/config.py:152-160`, `src/common/taskiq.py:13-15`, `src/main.py:111-123`, `src/common/database.py:63`, `src/common/__init__.py:5`, every `from src.common.config import settings`
- **Evidence:**
  - `taskiq.py` runs `init_settings(os.getenv("ENVIRONMENT", "dev"))` and `setup_logging()` **on import**. `src.api.auth.service` imports it (via `email_tasks`), so `config.settings` is rebound mid-import.
  - The probe shows that after `init_settings("unittest")` the process holds a UNITTEST object (main, service, database) and a DEV object that read `.env.local` (`config.settings`, `src.admin`).
  - In any process where `ENVIRONMENT` is unset (tests, ad-hoc scripts), `.env.local` is silently loaded.
  - `src.common.settings` is permanently `None`.
  - `import src.main` without `app.py` raises `AttributeError` (verified), so `uvicorn src.main:app`, `fastapi run` and `[tool.fastapi] entrypoint` cannot work.
  - `assert settings is not None` (main.py:111) disappears under `-O`.
- **Impact:** Non-deterministic configuration that depends on import order (admin credentials and secrets may come from the wrong object). It blocks Roadmap Phase 3 ("settings as dependency", `fastapi run`). It also prevents creating a test app with different settings.
- **Fix:**
  ```python
  @lru_cache
  def get_settings() -> Settings: return Settings()   # env vars > .env.local > defaults
  SettingsDep = Annotated[Settings, Depends(get_settings)]
  def create_app(settings: Settings | None = None) -> FastAPI: ...
  app = create_app()                                    # [tool.fastapi] entrypoint = "src.main:app"
  ```
  - `env_file=".env.local"` is safe under pydantic-settings because env vars take priority over dotenv; keep the file out of images.
  - Engine, Redis, broker, FastMail and admin are built in `create_app`/lifespan, never at import.
  - The taskiq broker module must not call `init_settings`. The worker uses `taskiq worker src.worker:broker`, built from `get_settings()`.
  - Use `SecretStr` for `JWT_SECRET_KEY`, `ADMIN_*`, `CACHE_PASSWORD`, `MAIL_PASSWORD`. Use `StrEnum` for `Environment`.
- **Effort:** M
- **Breaking:** no API change; launch command changes (`fastapi run`).
- **README:** Roadmap Phase 3 ("Settings as an explicit dependency", "no import-time side effects in taskiq.py and mail.py") is **confirmed with concrete evidence**. The cross-object split and the `.env.local` leak are NEW.

### BE-05: taskiq retries are a no-op; the broker is at-most-once; results are kept forever
- **Severity:** high
- **Category:** bug
- **Location:** `src/common/taskiq.py:17-23`, `src/utils/email_tasks.py:13-16,38-41`
- **Evidence:**
  - `retry_on_error=True` is read only by `SimpleRetryMiddleware`/`SmartRetryMiddleware` (grep of installed taskiq 0.12.2; taskiq docs "Middlewares"). No middleware is registered, so a failed email is never retried.
  - `TASKIQ_RETRY_COUNT` and `TASKIQ_RETRY_DELAY_SECONDS` are unused.
  - `ListQueueBroker` pops messages without acknowledgement. taskiq-redis offers `RedisStreamBroker`, which "supports acknowledgment of messages" (installed source). A worker crash loses the email.
  - `RedisAsyncResultBackend(...)` uses defaults `keep_results=True, result_ex_time=None`, so every task result is stored in Redis db 2 **without TTL**.
  - Per the taskiq FastAPI integration docs, the API process should `await broker.startup()`/`shutdown()` in its lifespan when `not broker.is_worker_process`. It never does.
- **Impact:** Verification and reset emails are lost on any transient SMTP or worker failure. The user sees "email sent" and nothing arrives. Redis memory grows without bound.
- **Fix:** `broker.with_middlewares(SmartRetryMiddleware(default_retry_count=settings.TASKIQ_RETRY_COUNT, default_delay=settings.TASKIQ_RETRY_DELAY_SECONDS, use_jitter=True, use_delay_exponent=True))`. Switch to `RedisStreamBroker`. Set `result_ex_time` (or drop the result backend for fire-and-forget tasks). Call broker startup/shutdown in the lifespan.

  Retries make delivery at-least-once, so duplicate emails become possible. That is acceptable for these templates; document it.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW. Known issue 10 should also list the unused `TASKIQ_RETRY_*` settings.

### BE-06: The database URL, including the password, is logged at startup
- **Severity:** high
- **Category:** bug (security)
- **Location:** `src/common/database.py:88`
- **Evidence:** `logger.info("db.pool_init", url=settings.DATABASE_URL)`. Line 41 masks the password correctly with `render_as_string(hide_password=True)`, but line 88 logs the raw string. With `OTLP_LOGS_ENABLED`, the password is shipped to Loki.
- **Impact:** A secret in logs (AGENTS.md "never log secrets"; OWASP Logging Cheat Sheet).
- **Fix:** Log `make_url(url).render_as_string(hide_password=True)` or nothing. Rotate the DB password in any environment where logs were collected.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW.

### BE-07: Admin "lock" is undone on the next login; lock, deactivate and revoke do not end existing sessions; `must_change` is never enforced
- **Severity:** high
- **Category:** logic
- **Location:** `src/admin.py:88-96,124-151`, `service.py:221-243,385-391`, `dependencies.py:340-352`
- **Evidence:**
  - Admin lock sets `is_locked=True` without `locked_until`. `authenticate_user` treats `locked_until is None` as "lock expired", unlocks and audits `ACCOUNT_UNLOCKED` (service.py:238-253).
  - Refresh never checks `user.is_active`/`is_locked` (service.py:385-391), so a deactivated user keeps receiving new refresh tokens. Access tokens are rejected only for `is_active`.
  - `must_change` (admin.py:124) is not read anywhere in `src/`.
  - Admin actions write no audit rows.
- **Impact:** The admin UI promises "They will not be able to log in" and does not deliver it. Incident response (lock a compromised account) is ineffective.
- **Fix:**
  - Separate `is_locked` (admin, indefinite) from `locked_until` (throttle). Alternatively, admin lock sets `locked_until='infinity'`.
  - Refresh and `get_current_user` reject locked or inactive users.
  - Lock, deactivate and revoke call a single `revoke_user_sessions(user_id, reason)` service function, which is also used for Known issue 5.
  - Enforce `must_change`: login returns a restricted token or 403 with code `PASSWORD_CHANGE_REQUIRED`.
  - Audit admin actions.
- **Effort:** M
- **Breaking:** the `must_change` enforcement adds a new error code (frontend impact).
- **README:** NEW (relates to D2 and Known issue 5).

### BE-08: Single-use email tokens can be used twice concurrently (TOCTOU)
- **Severity:** medium
- **Category:** bug
- **Location:** `crud.py:202-225`, `service.py:492-501` (reset), `524-537` (verify)
- **Evidence:** `get_valid_email_token` (SELECT) and `mark_email_token_used` (separate UPDATE without `used_at IS NULL`) run in separate transactions. Two parallel `/password/reset` requests with one token both succeed.
- **Impact:** Violates the single-use guarantee (OWASP Forgot Password Cheat Sheet: tokens single use). The real-world exploit is limited, but the invariant must hold for invitations (frontend task 1) too.
- **Fix:** `UPDATE email_tokens SET used_at=now() WHERE token_hash=:h AND purpose=:p AND used_at IS NULL AND invalidated_at IS NULL AND expires_at>now() RETURNING user_id, new_email`. On 0 rows, run one SELECT to classify the failure as `TOKEN_USED`/`TOKEN_EXPIRED`/`TOKEN_INVALID` (frontend task 3). Do this in the same transaction as the password update.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW.

### BE-09: Lockout counter has lost updates under parallel requests
- **Severity:** medium
- **Category:** bug
- **Location:** `service.py:267-295`
- **Evidence:** `user.failed_login_attempts += 1` is computed in Python on a row read without a lock, then committed. N parallel wrong passwords each read k and write k+1.
- **Impact:** An attacker gets many more than `MAXIMUM_LOGIN_ATTEMPTS` guesses per window by sending them in parallel.
- **Fix:** `UPDATE users SET failed_login_attempts = failed_login_attempts + 1 WHERE id=:id RETURNING failed_login_attempts`, then set the lock conditionally in the same statement or transaction. Long term, D4 rate limiting (Redis `INCR` with TTL) replaces the account counter.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW (relates to Phase 1 rate limiting).

### BE-10: Registration race returns 500; failed enqueue after commit leaves an orphaned account
- **Severity:** medium
- **Category:** bug
- **Location:** `service.py:157-197`, `router.py:99-107`
- **Evidence:**
  - The check-then-insert on username/email allows a concurrent duplicate to raise `IntegrityError`. The router's `except Exception` turns it into a 500 "Registration failed" instead of 409.
  - User, credential, token and audit rows are committed before `send_verification_email_task.kiq()`. If Redis or the broker is down, the client gets a 500 but the user exists, and a retry returns 409 "Username already taken".
- **Impact:** Wrong status codes and a confusing retry experience. Non-atomic writes are already noted in Roadmap Phase 4.
- **Fix:** Unit of work in the service (single transaction). Map `IntegrityError` on `uq_users_username`/`uq_users_email` to `UsernameTakenError`/`EmailAlreadyRegisteredError` (409, codes requested by frontend task 1).

  Enqueue after commit and do not fail the request on enqueue failure: log `auth.verification_enqueue_failed`, the user can resend. Better: a transactional outbox table drained by a taskiq cron, which gives exactly-once enqueue relative to the DB.
- **Effort:** M
- **Breaking:** status codes only (500 to 409/201).
- **README:** atomicity is confirmed in Roadmap Phase 4; the IntegrityError→500 and enqueue-after-commit parts are NEW.

### BE-11: Two error shapes on the wire
- **Severity:** medium
- **Category:** bug (contract)
- **Location:** `src/main.py:129-131` (no `HTTPException`/`StarletteHTTPException` handler)
- **Evidence:** Verified: 404 `{"detail":"Not Found"}`, 405 `{"detail":"Method Not Allowed"}`, missing bearer 401 `{"detail":"Not authenticated"}` (FastAPI ≥ 0.122 behaviour). All other errors use the envelope.
- **Impact:** The frontend `normalizeApiError()` must parse two formats. OpenAPI does not describe either.
- **Fix:** Register one handler for `StarletteHTTPException` and one for `AppError` (BE-29), both rendering the chosen D1 format.
- **Effort:** S
- **Breaking:** yes, the body of 404/405/401-missing changes. Frontend impact: small; `normalizeApiError` gains consistency.
- **README:** NEW. Phase 2 "one consistent handler" covers the intent but not this gap.

### BE-12: Unhandled 500s lose CORS, `X-Request-ID` and security headers
- **Severity:** medium
- **Category:** bug
- **Location:** `src/main.py:129` (`add_exception_handler(Exception, ...)`)
- **Evidence:** Verified: the 500 response has only `content-type`. Starlette routes `Exception` handlers to `ServerErrorMiddleware`, the outermost layer, outside CORS and all user middleware. The browser reports a CORS error instead of a 500. The body's `request_id` is a fresh random UUID that appears in no log. `generic_exception_handler` logs `error=str(exc)`, which for `IntegrityError` contains column values such as emails.
- **Impact:** 500s cannot be debugged from the client side, and PII can reach the logs.
- **Fix:** A small pure-ASGI catch-all middleware placed **inside** CORS that converts unexpected exceptions to the 500 error body, takes the request id from `scope["state"]`, and logs `exc_info` with `error_type` only. Keep the `Exception` handler as a last resort.
- **Effort:** S
- **Breaking:** no (adds headers).
- **README:** NEW.

### BE-13: Envelope metadata is wrong: `request_id` mismatch, naive timestamp, meaningless `execution_time`, unvalidated client request id
- **Severity:** medium
- **Category:** bug
- **Location:** `src/common/responses.py:118-136`, `src/common/schemas.py:48-51`, `src/common/middleware.py:471`
- **Evidence:**
  - Verified: the body `request_id` (`0c0e…`) is not the header `X-Request-ID` (`9950…`). The middleware generates the id but stores it only in structlog contextvars; `create_response_metadata` creates another `uuid4()`.
  - `timestamp` is `datetime.now().isoformat()`, local time without an offset (`2026-11-16T10:38:09.904435`), which is not RFC 3339.
  - `execution_time` is measured from a `start_time` taken inside the handler (about 1e-5 s).
  - The client-supplied `x-request-id` is echoed and logged unvalidated (log injection, unbounded size).
- **Impact:** Support correlation via the body id is impossible. The frontend may parse the timestamps as local time.
- **Fix:** The request-id middleware (pure ASGI, outermost) validates the incoming id (`^[A-Za-z0-9-]{8,64}$`, else generates one) and writes it to `scope["state"]["request_id"]`. Response builders read it from there. If the envelope survives D1: use `datetime.now(UTC)` and drop `execution_time` (the `X-Process-Time` header exists). With RFC 9457 and no success envelope (D1 recommendation), the metadata block disappears.
- **Effort:** S
- **Breaking:** fields change (frontend reads `metadata.request_id`? only for display).
- **README:** NEW. Roadmap Phase 2 has "RFC 3339 output" in general terms.

### BE-14: 401 responses from `AuthenticationError` drop `WWW-Authenticate`
- **Severity:** medium
- **Category:** bug
- **Location:** `src/common/exceptions.py:78-98`
- **Evidence:** Verified: bad-bearer 401 has no `WWW-Authenticate`. The exception carries `headers={"WWW-Authenticate": "Bearer"}`, but `error_response()` ignores them. RFC 9110 §15.5.2: a 401 response "MUST send a WWW-Authenticate header field".
- **Fix:** Pass `exc.headers` through. For an invalid token, use `Bearer error="invalid_token"` (RFC 6750 §3).
- **Effort:** S
- **Breaking:** no.
- **README:** NEW.

### BE-15: Login timing reveals whether a username exists
- **Severity:** medium
- **Category:** logic (security)
- **Location:** `service.py:215-218`
- **Evidence:** An unknown username returns before any Argon2 work (≈ 0 ms). A known one costs ≈ 41 ms (measured). Inactive and locked accounts also return before verify, each with distinct messages; the message part is already in the Roadmap.
- **Impact:** Account enumeration (OWASP Authentication Cheat Sheet, "Authentication responses"; AGENTS.md "no user enumeration through ... timing").
- **Fix:** Verify against a module-level dummy hash (`_DUMMY_HASH = _ph.hash(secrets.token_urlsafe())`, computed once in the lifespan) when the user is unknown, inactive or locked. Then return one uniform `InvalidCredentialsError`.
- **Effort:** S
- **Breaking:** message change (frontend shows backend message on login, Phase 2).
- **README:** Phase 1 covers uniform messages and forgot-password timing; **login timing is NEW**.

### BE-16: Logout is not idempotent and does not clear the cookie on failure
- **Severity:** medium
- **Category:** logic
- **Location:** `service.py:419-442`, `router.py:170-206`, `dependencies.py:405-411`
- **Evidence:** An unknown or already-revoked session returns 400 "Session not found" and **no `Set-Cookie` deletion**. A missing cookie returns 401. Refresh failures (router.py:230-237) also leave the dead cookie in place.
- **Impact:** The client keeps a dead cookie, and every reload retries the refresh. Logout should succeed whenever the end state is "logged out".
- **Fix:** `POST /auth/logout` always returns 204 and deletes the cookie (revoke if found, audit). Refresh 401 responses also delete the cookie.
- **Effort:** S
- **Breaking:** status 200 to 204 (frontend: response is "ignored", so no impact).
- **README:** NEW.

### BE-17: Fake authorisation and preference data in `UserResponse`
- **Severity:** medium
- **Category:** logic (contract)
- **Location:** `service.py:70-89`
- **Evidence:** `roles=["test"]`, `permissions=["posts:create","posts:read"]`, `preferences={"locale":"en-US","theme":"dark","timezone":"UTC"}`, `profile_picture_url=""`, returned to every user.
- **Impact:** The frontend may gate UI on permissions the backend never enforces, and may apply a theme the user never chose. The field types are untyped `list[str]`/`dict`. This is excessive or incorrect data exposure in the sense of OWASP API3.
- **Fix:** Remove these fields until D3 (RBAC) is decided. Alternatively, return `roles: []`, `permissions: []`, `preferences: null`, `profile_picture_url: null`, and type them (`list[Role]`, `UserPreferences | None`).
- **Effort:** S
- **Breaking:** yes; frontend `User` type (mirrors `UserResponse`).
- **README:** NEW (relates to D3).

### BE-18: Sensitive account changes lack re-authentication, attempt limits and notification
- **Severity:** medium
- **Category:** logic (security)
- **Location:** `service.py:578-597`, `user/service.py:50-72`
- **Evidence:**
  - `PATCH /user/me/password` has no attempt counter, so an attacker holding an access token can guess `current_password` without lockout.
  - `PATCH /user/me` email change requires no current password, and the old address is never notified.
  - The new address is not reserved, so the eventual confirm endpoint (Known issue 6) must re-check uniqueness and handle the unique violation.
- **Impact:** A stolen access token (15 min) becomes a path to account takeover: change the email, confirm it, then reset the password.
- **Fix:**
  - Count failed `current_password` attempts on the same counter or rate limiter as login (D4).
  - Require `current_password` for an email change. Send a notification with an "undo/revoke" link to the old address.
  - The confirm step runs in one transaction: the conditional token consume (BE-08), the user update, and an `EMAIL_CHANGED` audit row.
- **Effort:** M
- **Breaking:** `PATCH /user/me` with `email` needs `current_password` (frontend impact: `useUpdateMe`, no UI yet).
- **README:** extends Known issue 6 (confirmed) and Phase 1. The re-authentication and notification parts are NEW.

### BE-19: Mail ignores `Settings`
- **Severity:** medium
- **Category:** bug
- **Location:** `src/common/mail.py:18-54,58`
- **Evidence:**
  - Config is read via `os.getenv`, but pydantic-settings does not export `.env.local` into `os.environ`, so `MAIL_*` in `.env.local` are ignored in dev.
  - `Settings.MAIL_STARTTLS`/`MAIL_PORT` are ignored (STARTTLS is hard-coded).
  - FastMail is built at import time.
  - The startup log includes `MAIL_USERNAME` and uses a non-`area.action` event name.
- **Impact:** Mail cannot be configured the documented way, and the configuration is duplicated.
- **Fix:** Build `FastMail` from `Settings` in a lazily cached factory (worker side). Use `SecretStr` for the password. Log `mail.configured` with server and port only.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW. Known issue 10 should list `MAIL_*` as effectively unused.

### BE-20: No route declares a response model; OpenAPI is empty
- **Severity:** medium
- **Category:** best-practice / modernisation
- **Location:** all routers (`auth/router.py`, `user/router.py`)
- **Evidence:**
  - The OpenAPI probe shows that every auth/user operation has an empty 200 schema, and none documents 400/401/409/500.
  - The routes return `JSONResponse`, so FastAPI 0.130's Rust-side Pydantic serialisation ("2x (or more) performance", release notes 0.130.0) and output filtering never apply.
  - `top_level_fields` duplicates `access_token` (router.py:143-146).
- **Impact:** No usable contract. Frontend types are hand-mirrored (mismatches 1–10 in backend-contract.md came from this). Generated clients are impossible.
- **Fix:** Typed returns (`-> TokenResponse`, `-> UserResponse`), `status_code`, `summary` and `responses=error_responses(401, 409)` (helper, see §6). Login returns RFC 6749 §5.1 `{access_token, token_type, expires_in}` at the top level, which also makes Swagger's password flow work without the hack.
- **Effort:** M
- **Breaking:** yes if the success envelope is dropped (D1).
- **README:** Roadmap Phase 2 is **confirmed**; the evidence is new.

### BE-21: Dead null-check in `decode_token`; a malformed `sub` gives a 500
- **Severity:** low
- **Category:** bug
- **Location:** `src/utils/jwt_utils.py:57-65`, `dependencies.py:345`
- **Evidence:** `str(payload.get("sub"))` turns `None` into `"None"`, so the `is None` check never fires. `uuid.UUID("None")` in `get_current_user` raises `ValueError`, which becomes a 500. The broad `except Exception` hides programming errors.
- **Fix:** `jwt.decode(..., algorithms=["HS256"], options={"require": ["exp","iat","sub","jti"]}, issuer=..., audience=...)`, then validate into `TokenData` with `sub: UUID`. Catch only `jwt.PyJWTError` and `ValidationError` and map them to 401.
- **Effort:** S
- **Breaking:** no.
- **README:** iss/aud/typ is in Phase 1; the dead check and the 500 are NEW.

### BE-22: `PATCH /user/me` cannot clear `display_name`; unknown fields are ignored
- **Severity:** low
- **Category:** logic
- **Location:** `user/service.py:47-48`, `auth/schemas.py:339-352`
- **Evidence:** `if display_name is not None` treats an explicit `null` as "unchanged". The model lacks `extra="forbid"`.
- **Fix:** Use `body.model_dump(exclude_unset=True)` (or `model_fields_set`) for PATCH semantics (RFC 7396-style merge) and `ConfigDict(extra="forbid")`.
- **Effort:** S
- **Breaking:** `{"display_name": null}` now clears the name (frontend: document it).
- **README:** `extra="forbid"` is in Phase 1; the PATCH semantics are NEW.

### BE-23: `GET /` cache decorator returns 500 when route caching is off and caches per-request metadata
- **Severity:** low
- **Category:** bug
- **Location:** `src/main.py:184-198`, `src/common/cache.py:160-164`
- **Evidence:** With `ROUTE_CACHE_ENABLED=false`, `cache.setup()` is skipped, but `@cache` still wraps `/`. cashews raises `NotConfiguredError("run cache.setup(...)")` (installed source, wrapper.py:89), giving a 500 (verified without lifespan). When caching is on, the cached `JSONResponse` replays the first request's `request_id`/`timestamp`.
- **Fix:** Always call `cache.setup(...)` and use `cache.disable()` when route caching is off. Do not cache envelope metadata (moot after D1).
- **Effort:** S
- **Breaking:** no.
- **README:** NEW.

### BE-24: The lifespan is not exception-safe, and resources are created at import time
- **Severity:** low
- **Category:** best-practice
- **Location:** `src/main.py:51-103`, `database.py:63`, `admin.py:325`, `mail.py:58`, `taskiq.py:17`
- **Evidence:**
  - No `AsyncExitStack`: if Redis fails after the DB starts, the engine is not disposed.
  - `app.state.cache.close()` is deprecated in redis-py 7.4 (`@deprecated_function(version="5.0.1", reason="Use aclose() instead")`).
  - The broker never starts or stops (BE-05).
  - `start_time = datetime.now()` is naive.
  - The engine, admin, FastMail and broker are all built at import time.
- **Fix:** Lifespan with `AsyncExitStack` that **yields typed state** (Starlette lifespan state; typed `Request[State]` since Starlette 0.52.0):
  ```python
  class AppState(TypedDict): engine: AsyncEngine; sessionmaker: async_sessionmaker[AsyncSession]; redis: Redis
  async def lifespan(app) -> AsyncIterator[AppState]: ...
  ```
  Expose it through dependencies `DbSession`, `RedisDep = Annotated[Redis, Depends(get_redis)]`. Routers stop reading `request.app.state.cache`.
- **Effort:** M
- **Breaking:** no.
- **README:** partly Phase 3 (graceful shutdown); the specifics are NEW.

### BE-25: Deprecated `HTTP_422_UNPROCESSABLE_ENTITY`; the warning is hidden
- **Severity:** low
- **Category:** modernisation
- **Location:** `src/common/exceptions.py:74`, `pyproject.toml` `filterwarnings = ["ignore::DeprecationWarning", ...]`
- **Evidence:** Starlette 0.52.1 `status.py:184-193` maps it to `HTTP_422_UNPROCESSABLE_CONTENT` (RFC 9110 naming, Starlette 0.48.0). The blanket `ignore::DeprecationWarning` will also hide the FastAPI 0.14x deprecations (`root_path`, 0.146.0) during the upgrade.
- **Fix:** Use `HTTP_422_UNPROCESSABLE_CONTENT`. Change the filter to `error::DeprecationWarning:src.*` (or the default) so our own deprecations fail the tests.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW.

### BE-26: Access log runs outside the request-id scope
- **Severity:** low
- **Category:** bug (assembly; owner observability)
- **Location:** `src/main.py:138-161`, `middleware.py:461-491`
- **Evidence:** `MetaDataMiddleware` is added first, so it is the innermost user middleware. `AccessLogMiddleware` (outermost) emits `http.access` in a context that never had `request_id`/`user_id` bound, and MetaData clears contextvars before returning. All are `BaseHTTPMiddleware`.
- **Fix:** One pure-ASGI request-context middleware as the outermost layer (id, contextvars, access log, timing). Roadmap Phase 3 covers the ASGI rewrite.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW detail under Phase 3 (middleware order change needs approval per AGENTS.md).

### BE-27: 2FA stubs are published in the contract
- **Severity:** low
- **Category:** best-practice
- **Location:** `auth/router.py:415-448`
- **Evidence:** Three POST routes return 501 with error code `INTERNAL_ERROR` and appear in OpenAPI.
- **Fix:** Remove them; a boilerplate should not ship unimplemented contract.
- **Effort:** S
- **Breaking:** removes 3 paths (the frontend does not use them).
- **README:** NEW.

### BE-28: Identifier normalisation and input bounds
- **Severity:** low
- **Category:** logic
- **Location:** `auth/schemas.py:291-330`
- **Evidence:** Usernames are case-sensitive (`Alice` ≠ `alice`, both registrable; login is case-sensitive). `token: str` (reset/verify) and `PasswordForgotRequest.email` have no format or length bound.
- **Fix:** Use a case-insensitive unique index (`lower(username)` or `citext`; coordinate with db-engineer) and NFKC + casefold normalisation. Add `token: str = Field(min_length=20, max_length=128)`.
- **Effort:** S
- **Breaking:** registration could reject case-only duplicates.
- **README:** NEW.

### BE-29: Feature modules are hard to extend
- **Severity:** medium
- **Category:** extensibility
- **Location:** `src/api/user/*` (pure re-export shims), `auth/dependencies.py:380-397`, all routers
- **Evidence:**
  - `auth` owns `User` plus profile logic, and `user` re-exports it.
  - Business logic with writes (lockout, audit) runs inside a dependency (`authenticate_form_user`).
  - Services return `(None, "msg")` tuples. Routers wrap everything in `except Exception` and pick status codes by hand.
  - There is no domain-exception base, pagination helper, settings dependency or versioned router.
- **Impact:** Every new module copies about 30 lines of try/except and envelope code per route. Status-code choice is scattered.
- **Fix:** See §6 (template, exception hierarchy, settings dependency, versioning, pagination).
- **Effort:** L (spread over phases)
- **Breaking:** internal only.
- **README:** Roadmap Phase 0 and 2 (named exceptions, template module) **confirmed**; the concrete design is in §6.

### BE-30: `mypy src` fails
- **Severity:** low
- **Category:** best-practice
- **Location:** `src/admin.py:90,107,126,145,243,291`
- **Evidence:** 6 `arg-type` errors (`form.getlist("pk")` returns `UploadFile | str`).
- **Fix:** `ids = [uuid.UUID(pk) for pk in form.getlist("pk") if isinstance(pk, str)]`.
- **Effort:** S
- **Breaking:** no.
- **README:** NEW. Known issue 13 is confirmed and Known issue 14 is **not reproducible** (see §5).

### BE-31: Refresh cookie path, versioning and cookie prefix are not designed together
- **Severity:** medium
- **Category:** modernisation (contract)
- **Location:** `service.py:97-114`, `main.py:172-175`
- **Evidence:** `Path=/auth` is hard-coded, and behind Traefik's `/api` strip prefix the browser never sends the cookie (frontend task 4). The Roadmap plans `/api/v1` and a `__Host-` prefix, but `__Host-` requires `Path=/` (RFC 6265bis cookie prefixes), which would send the refresh token with every API request. FastAPI 0.146.0 deprecated `FastAPI(root_path=...)` in favour of `fastapi run --root-path`.
- **Fix:** Mount routers under `settings.API_PREFIX` (`/api/v1`) **inside the app** and stop stripping the prefix in Traefik (coordinate with deploy-engineer). Name the cookie `__Secure-refresh_token` (prefix allows a path), with `Path=f"{API_PREFIX}/auth"`, `Secure`, `HttpOnly`, `SameSite=Strict` and `Max-Age=REFRESH_TOKEN_EXPIRE_DAYS*86400`. Set and delete with the same helper.
- **Effort:** S
- **Breaking:** yes; frontend `VITE_API_BASE_URL`, cookie name invisible to JS.
- **README:** frontend task 4 is **missing from the backend Roadmap** (NEW). Known issue 8 is confirmed.

### BE-32: Redis is effectively the session authority; raw refresh tokens are Redis keys
- **Severity:** medium
- **Category:** logic
- **Location:** `service.py:342-352,394-416`, `crud.py:39-40`
- **Evidence:**
  - The slow path only detects reuse and never accepts a valid unrevoked DB session, so a Redis flush or restart logs every user out.
  - A Redis `SET` failure after the DB commit at login leaves an unusable session row.
  - The key is `rt:<raw token>`, which contradicts "tokens at rest are SHA-256" (AGENTS.md Security rules).
- **Fix:** Covered by BE-01 (Postgres authority; any cache keyed by hash).
- **Effort:** included in BE-01
- **Breaking:** no.
- **README:** NEW. It contradicts the Design decisions row "Session store".

**Severity tally:**

| Severity | Count | Findings |
|---|---|---|
| critical | 1 | BE-01 |
| high | 6 | BE-02, BE-03, BE-04, BE-05, BE-06, BE-07 |
| medium | 16 | BE-08, 09, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 29, 31, 32 |
| low | 9 | BE-21, 22, 23, 24, 25, 26, 27, 28, 30 |
| **total** | **32** | |

**Cross-area observations (not counted; owned by other agents):**
- `MetricsMiddleware._get_path_template` runs before routing and uses the raw path. Scanner 404s create unbounded `endpoint` label values (Prometheus cardinality). Owner: observability.
- `get_current_user` binds `username` into the log context, and the JWT carries `username` (PII in a readable token). Owner: observability and security.

---

## 5. Known issues 1–14: verification

| # | Status | Evidence |
|---|---|---|
| 1 | **confirmed** | `cache.py:28-35` uses `CACHE_HOST/PORT`; `conftest.py:42` sets only `REDIS_URL`. Taskiq URLs are not overridden either, so tests enqueue to `localhost:6379/1`. |
| 2 | **confirmed** | `conftest.py:60-76`: `alembic upgrade` plus `create_all`; `asyncio.run` on the import-time engine (caused by BE-04/BE-24). |
| 3 | **confirmed** | `metrics/router.py:31` `keys("rt:")`; no wildcard. |
| 4 | **confirmed** | `middleware.py:324` compares with `"/metrics"`; the route is `/metrics/`. |
| 5 | **confirmed** | `service.py:481-506,578-597`: no session revocation. Also ineffective until BE-01 is fixed, because revocation does not touch Redis. |
| 6 | **confirmed** | `user/service.py:59-72`. The link goes to the verify template and `/auth/verify-email`, where `verify_email` looks for purpose `VERIFY_EMAIL`, so clicking it fails with "Invalid or expired". See BE-18. |
| 7 | **confirmed** | `email_tasks.py:44`. The verify link path `/auth/verify-email` also does not match the frontend routes (frontend task 2). |
| 8 | **confirmed** | `service.py:101-107`; also no `max_age` (session cookie). |
| 9 | **confirmed** | `pyproject.toml` addopts 80 vs `fail_under = 85`. |
| 10 | **partly wrong / incomplete** | `BCRYPT_ROUNDS` is unused (confirmed). `CACHE_KEY_PREFIX` exists only in the `.env` template, **not in `Settings`**. `REDIS_URL` is confirmed. Also unused: `TASKIQ_METRICS_PORT`, `TASKIQ_RETRY_COUNT`, `TASKIQ_RETRY_DELAY_SECONDS`, `ROUTE_CACHE_TIME_CONDITION`, `DEBUG`, and effectively all `MAIL_*` (BE-19). |
| 11 | confirmed (db-engineer area) | `alembic/env.py:39` `load_dotenv(..., override=True)`. |
| 12 | confirmed in app code (Dockerfile out of scope) | Health lives under the metrics router, which is gated by `METRICS_ENABLED` (`main.py:168`). |
| 13 | **confirmed** | `ruff format --check` fails on `src/admin.py`. |
| 14 | **not reproducible (likely fixed)** | `uv run --frozen --no-sync python` works; `.venv` is CPython 3.14.4. |

README corrections to make:
- Design decisions "Session store" (BE-32).
- The "What you get" FastAPI version.
- The Known issues header "None are fixed yet" (Known issue 14).
- Roadmap Phase 2 "409 on payload mismatch" should be **422** (see D9).
- Add frontend tasks 4 and 5 to the Roadmap.

---

## 6. Modern FastAPI (verified 2026-11-16) and extensibility design

### 6.1 Release features that matter here

Sources: fastapi.tiangolo.com/release-notes and github.com/fastapi/fastapi/releases (checked 2026-11-16); installed source for 0.136.1.

| Version | Feature (source wording) | Installed? | Use here |
|---|---|---|---|
| 0.118.0 | yield-dependency exit code "now runs after the response is sent" | yes | **Do not commit in `get_db` teardown.** A failed commit would follow an already-sent 200. The service commits, or the dependency uses `scope="function"`. |
| 0.121.0 | `Depends(scope="function" \| "request")` (installed docstring: `"function"` ends **before** the response is sent) | yes | Use for a unit-of-work dependency if chosen. |
| 0.122.0 | security classes return 401 when credentials are missing | yes | Verified. Needs the BE-11 handler for a consistent body. |
| 0.130.0 | "Serialize JSON response with Pydantic (in Rust)" when a return type or response_model exists, "2x (or more)" | yes | Unused: all routes return `JSONResponse` (BE-20). |
| 0.131.0 | `ORJSONResponse`/`UJSONResponse` deprecated | yes | Not used (good). |
| 0.132.0 | `strict_content_type` on by default | yes | Keep. |
| 0.134/0.135 | JSON Lines / SSE streaming with `yield` | yes | Available for future feeds; not needed now. |
| 0.135.2 docs | `[tool.fastapi] entrypoint = "module:app"` (FastAPI CLI docs) | needs `fastapi-cli` (not installed) | Blocked by BE-04. |
| 0.137.0 | `router.routes` becomes a tree (breaking); per-router dependencies, handlers and middleware are "future features enabled" | no | Do not iterate `app.routes` in code or tests; use `iter_route_contexts()` (0.137.2). |
| 0.142.0 / 0.143.0 | native OpenTelemetry; auto exporter setup opt-in (`FastAPI(telemetry={"auto_configure": True})` / `FASTAPI_OTEL_AUTO_CONFIGURE`) | no | Roadmap Phase 3 (observability owns). |
| 0.146.0 | `FastAPI(root_path=)` deprecated; use `fastapi run --root-path` | no | Mount `/api/v1` via `include_router` (BE-31), not root_path. |
| 0.148 / 0.151 / 0.162.0 | defaults no longer validated unless `validate_default`; JSON `null` distinguished from omitted; form bool defaults restored (breaking) | no | Re-test PATCH and form-login semantics on upgrade (the exact scope of 0.151 was not verified against code). |
| 0.155.0 | unknown kwargs on `Query`/`Path`/… raise `TypeError` | no | grep shows no usage. |
| 0.162.5 / 0.162.7 | fix sync/async yield-dependency cleanup **on cancellation** | no | Relevant to `get_db` rollback on client disconnect: an upgrade reason. |
| 0.162.6 | drops Python 3.10 | no | n/a (3.14). |
| 0.163.0 | `Depends(max_concurrency=...)` | no | Candidate for the Argon2 limit (BE-03); semantics not yet documented, so this is unverified. |

Starlette (GitHub `Kludex/starlette` release notes): 0.52.0 typed `State`/`Request[State]`; 1.0.0rc1 removed `on_event`/`on_startup` (not used here, good); **1.6.0 "Add `max_body_size` to `Starlette` and route classes"** (Roadmap Phase 1 "request limits"); 1.8.0 requires Python ≥ 3.11.

**Upgrade proposal** (needs approval): fastapi 0.136.1 → 0.163.1, starlette 0.52.1 → 1.10.x, add `fastapi-cli`, pydantic → 2.14.3 (FastAPI's lock). Do it as one PR before the contract work. Fix `filterwarnings` first (BE-25) so that deprecations surface.

### 6.2 Domain exception hierarchy (one handler, D1-agnostic)

```python
# src/common/errors.py
class AppError(Exception):
    status: ClassVar[int] = 500
    code: ClassVar[str] = "INTERNAL_ERROR"          # stable, machine-readable; frontend maps codes
    title: ClassVar[str] = "Internal server error"
    headers: ClassVar[Mapping[str, str]] = {}
    def __init__(self, detail: str | None = None, **extensions: object) -> None:
        self.detail, self.extensions = detail, extensions

class BadRequestError(AppError):            status, code = 400, "BAD_REQUEST"
class AuthenticationRequiredError(AppError): status, code = 401, "UNAUTHORIZED"; headers = {"WWW-Authenticate": "Bearer"}
class PermissionDeniedError(AppError):       status, code = 403, "FORBIDDEN"
class NotFoundError(AppError):               status, code = 404, "NOT_FOUND"
class ConflictError(AppError):               status, code = 409, "CONFLICT"
class PreconditionFailedError(AppError):     status, code = 412, "PRECONDITION_FAILED"
class RateLimitedError(AppError):            status, code = 429, "RATE_LIMITED"   # + Retry-After
class ServiceUnavailableError(AppError):     status, code = 503, "SERVICE_UNAVAILABLE"

# src/api/auth/exceptions.py
class InvalidCredentialsError(AuthenticationRequiredError): code = "INVALID_CREDENTIALS"
class TokenInvalidError(BadRequestError): code = "TOKEN_INVALID"
class TokenExpiredError(BadRequestError): code = "TOKEN_EXPIRED"
class TokenUsedError(BadRequestError):    code = "TOKEN_USED"
class UsernameTakenError(ConflictError):  code = "USERNAME_TAKEN"
class EmailAlreadyRegisteredError(ConflictError): code = "EMAIL_ALREADY_REGISTERED"
```

One renderer, `render_error(request, status, code, title, detail, extensions, headers)`, is used by the handlers for `AppError`, `StarletteHTTPException`, `RequestValidationError`, and the catch-all middleware (BE-12). Switching from the envelope to RFC 9457 changes only `render_error`. A helper `error_responses(*statuses) -> dict[int, dict]` feeds the `responses=` field in OpenAPI.

### 6.3 Feature-module template

```
src/api/v1/__init__.py        api_router = APIRouter(prefix=settings.API_PREFIX); includes each feature router
src/api/<feature>/
  __init__.py                 from .router import router
  router.py                   router = APIRouter(prefix="/<features>", tags=["<features>"]); thin, typed returns
  schemas.py                  XCreate / XUpdate (extra="forbid"), XResponse (from_attributes=True), XPage = Page[XResponse]
  service.py                  business rules; owns the transaction (async with db.begin()); raises AppError subclasses;
                              BOLA check: every get/update/delete filters by owner_id = current_user.id
  repository.py (crud.py)     select/insert/update; never commits
  models.py, exceptions.py, dependencies.py (valid_x: Annotated[X, Depends(get_owned_x)] → 404 if not owned)
tests/<feature>/test_router.py (happy, 422, 401 missing/invalid/expired, 404/409, side effect)
```

Rules:
- No business writes in dependencies.
- Routers never catch `Exception`.
- Feature folders import only `src.common` plus their own module, or another feature's **service** (not its crud).
- Split identity (`users`: model, profile) from `auth` (credentials, sessions, tokens). `auth` depends on `users`, not the reverse.

### 6.4 Settings as a dependency

`get_settings()` with `@lru_cache`, `SettingsDep = Annotated[Settings, Depends(get_settings)]`, and `create_app(settings)`. Tests use `create_app(Settings(_env_file=None, ...))` or `app.dependency_overrides[get_settings]`. Values needed by decorators at import time (`cache_control_ttl(default=settings.ROUTE_CACHE_TTL)`) move to call-time lookups or `cache.setup` defaults.

### 6.5 Versioning

Use the `API_PREFIX="/api/v1"` setting and a single `api_router`. Keep `/health/*` and `/metrics` unversioned. Set the cookie path from the prefix (BE-31). A future `v2` is a second router package that reuses services. Announce deprecations with `Deprecation` (RFC 9745) and `Sunset` (RFC 8594) headers from a small dependency attached to the old router (`APIRouter(dependencies=[Depends(deprecated(sunset=...))])`).

### 6.6 Pagination helper

```python
class Page[T](BaseModel):           # PEP 695 generics, py3.12+
    items: list[T]
    next_cursor: str | None
Limit = Annotated[int, Query(ge=1, le=100)]
async def paginate(stmt: Select, *, limit: int = 20, cursor: str | None, key=(Model.created_at, Model.id)) -> Page
```

Keyset pagination on `(created_at, id)` uses an opaque base64url cursor and a stable order. With UUIDv7 (D10), `id` alone is time-ordered. An RFC 8288 `Link: <...>; rel="next"` header is optional. No `total` by default (`COUNT(*)` gets expensive as tables grow).

---

## 7. Open decisions

### D1: error format. Recommendation: **RFC 9457 for errors, plain typed bodies for success, shipped together with `/api/v1`.**

| Option | For | Against |
|---|---|---|
| Keep envelope | no frontend change | Non-standard. It blocks FastAPI's response_model serialisation and OpenAPI typing (BE-20), because every success shape is `SuccessResponse{data: Any}`. Its metadata is currently wrong (BE-13). Two shapes already exist (BE-11). |
| RFC 9457 errors + envelope success | errors standard | Keeps the untyped success wrapper; half the migration cost for little gain. |
| **RFC 9457 errors + plain success** | IETF standard (RFC 9457, obsoletes 7807): `application/problem+json`, members `type`/`title`/`status`/`detail`/`instance`, extension members allowed (§3.2), validation example with `errors[]` + JSON Pointer. Generated clients and gateways understand it. Typed OpenAPI. Frees the 0.130 serialisation path. | One breaking change for the frontend (`normalizeApiError`, `data.user` → body, login `data.access_token` → top-level `access_token`, which the frontend already accepts). FastAPI has no built-in Problem Details (no entry through 0.163.1), so we own about 80 LOC. |

Shape: `{"type": "https://<docs>/problems/token-expired" | "about:blank", "title", "status", "detail", "instance": request path, "code": "TOKEN_EXPIRED", "request_id", "errors": [{"pointer": "/username", "detail", "code"}]}`.
- `code` stays as an extension member, because the frontend "maps codes, not statuses" (backend-contract.md).
- No new dependency is needed.
- Ship it in the same breaking window as `/api/v1` and BE-17, so the frontend migrates once.

### D9: idempotency keys. Recommendation: **implement in Phase 2, after the unit-of-work change, opt-in per route, Postgres-backed record plus Redis in-flight lock; header `Idempotency-Key`.**

Status checked 2026-11-16: `draft-ietf-httpapi-idempotency-key-header-08` (2026-10-22, active WG draft, not an RFC). Its normative behaviour:
- the value is an sf-string;
- the key must not be reused with a different payload;
- missing required key: **400**;
- **key reused with a different payload: 422**. The README Roadmap's "409 on payload mismatch" is wrong;
- **concurrent request in progress: 409**;
- after completion, the server SHOULD respond consistently with the original;
- the expiry policy must be published.

Design:
- Scope: `(user_id or "anon:" + route, key)`.
- Fingerprint: SHA-256 of method, route template and canonical body.
- In-flight lock: Redis `SET NX PX`, which gives 409.
- Store `(status, headers whitelist, body)` in an `idempotency_keys` table **in the same transaction as the mutation**. That gives exactly-once for DB effects; a Redis-only store has a window between commit and record.
- TTL 24 h with nightly purge (taskiq cron). Response header `Idempotency-Key` echoed.

Use it for:
- `register`;
- `password/reset`, `email/verify`, and the invitation accept endpoint. This lets the frontend retry single-use-token calls safely, which it currently refuses to do (backend-contract.md line 23).
- future domain POSTs.

**Exclude** `login`/`refresh`, whose responses carry credentials and would replay rotated tokens; BE-02's reuse interval covers those.

Trade-offs: Redis-only is simpler and enough when handlers are naturally idempotent, but it cannot guarantee replay after a crash between commit and store. Doing nothing leaves the frontend's `x-idempotency-key` header dead.

Frontend impact: rename `x-idempotency-key` to `Idempotency-Key` (it is already planned, mismatch 6). Coordinate the table with db-engineer.

Other decisions touched: D3 (BE-17 placeholders), D4 (BE-03/09/18 depend on rate limiting), D2 (BE-07 admin semantics), D10 (pagination cursor).

---

## 8. Proposed implementation order

1. **Hotfixes (S, no contract change):** BE-06 (DB URL in logs, plus rotate the credential), BE-30 (mypy), Known issue 13 (format), BE-25 (warnings filter).
2. **Assembly foundation (M):** BE-04 (settings dependency, `create_app`, no import-time side effects) and BE-24 (lifespan with typed state, `AsyncExitStack`). This unblocks `fastapi run` and isolated tests (helps Known issues 1 and 2).
3. **Auth correctness (M–L, with backend-tester and db-engineer):**
   - BE-01 + BE-32 + BE-02 (Postgres-authoritative atomic refresh rotation, reuse interval = frontend task 5);
   - Known issue 5 and BE-07 (single `revoke_user_sessions`, admin lock semantics, `must_change`);
   - BE-08, BE-09 (atomic token consume, atomic counter);
   - BE-03 + BE-15 (Argon2 off-loop, dummy hash).

   Regression tests for each race; the reuse test must present only the newest token.
4. **Jobs and mail (S):** BE-05 (retry middleware, stream broker, result TTL, broker lifecycle), BE-19 (mail from Settings), BE-10 (IntegrityError → 409, enqueue after commit, or an outbox).
5. **Upgrade (S–M, approval needed):** FastAPI 0.163.1, Starlette 1.10.x, `fastapi-cli`, `[tool.fastapi] entrypoint`, `max_body_size`. Re-run the suite to catch the 0.148/0.151/0.162 semantic changes.
6. **Contract window (L, breaking, one coordinated frontend release):**
   - D1 → RFC 9457 + `AppError` hierarchy (BE-11, 12, 13, 14, 29);
   - response models everywhere (BE-20);
   - remove placeholders and 2FA stubs (BE-17, BE-27);
   - `/api/v1` + `__Secure-` cookie with derived path (BE-31, frontend task 4);
   - idempotent logout (BE-16);
   - distinct token error codes (frontend task 3);
   - PATCH semantics (BE-22), email-change completion with re-auth and notification (Known issue 6, BE-18).

   Then publish the OpenAPI diff to the frontend.
7. **Extensibility (M):** module template replacing `exampleRoute`, `users`/`auth` split, pagination helper, unit of work with db-engineer, then **D9 idempotency** on top of it.
8. **Remaining low items:** BE-21, 23, 26 (with observability), 28.

## 9. Open points
- The race findings need executable proofs (backend-tester): concurrent `httpx.AsyncClient` calls against the Testcontainers stack.
- Approval needed for: dependency upgrades (§6.1), middleware order change (BE-26, BE-12), cookie name/path/SameSite change (BE-31), the Traefik strip-prefix change (deploy-engineer).
- Not verified: `Depends(max_concurrency)` overflow semantics (0.163.0); the exact scope of FastAPI 0.151 "null vs omitted" for JSON bodies; GIL release in argon2-cffi (inferred from CFFI semantics and the measurement).

Sources (checked 2026-11-16): https://fastapi.tiangolo.com/release-notes/ ; https://github.com/fastapi/fastapi/releases ; https://fastapi.tiangolo.com/fastapi-cli/ ; https://raw.githubusercontent.com/Kludex/starlette/main/docs/release-notes.md ; https://taskiq-python.github.io/framework_integrations/taskiq-with-fastapi.html ; https://taskiq-python.github.io/available-components/middlewares.html ; https://datatracker.ietf.org/doc/draft-ietf-httpapi-idempotency-key-header/ ; RFC 9457, RFC 9110, RFC 6750, RFC 6749, RFC 9700, RFC 6265bis, RFC 9745, RFC 8594, RFC 8288 (from memory of the published texts, not re-fetched).
