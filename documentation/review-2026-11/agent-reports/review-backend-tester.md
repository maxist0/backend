# Backend test-suite review (backend-tester), 2026-11-16

Mode: analysis only, no repo file changed. Docker is NOT available on this machine (daemon pipe `dockerDesktopLinuxEngine` missing; no Redis on 6379 checked via netstat: nothing listening). `uv run pytest` therefore could NOT be run: `pytest_configure` starts Postgres and Redis containers and would error before collection. All test findings below are static analysis; marked "inferred" where a runtime run would be needed.

## 1. Run outcome

| Check | Result |
|---|---|
| `uv run pytest` | not run (no Docker). Last known coverage artefact `htmlcov/` is from 2026-03-01, stale, not usable. No `.coverage` file. |
| Venv | healthy: `uv run --no-sync python` -> 3.14.4; `uv run mypy` works. Known issue 14 is NOT reproducible on this machine now. No `uv sync` needed, none run. |
| `uv run mypy src` | 6 errors in 1 file (`src/admin.py` lines 90,107,126,145,243,291: `UUID(UploadFile | str)` arg-type). 48 files checked. Not exit 0, so Definition of done is currently red. |
| `uvx ty check src` | 15 diagnostics (e.g. `src/common/middleware.py:169` expected `str`, found `object`). Only the tail was inspected; breakdown per rule not captured. |
| `uvx ruff@0.15.0 check tests` | all checks passed (default rule set; Phase 0 strict set not enabled). |
| Test inventory | 1108 test LOC, about 107 test functions in 4 files: auth 564 LOC, user 465, metrics 50 (2 tests), root 29 (1 test). 0 unit tests. |

## 2. Findings

Severity scale: critical / high / medium / low.

### TST-01 high, bug (coverage config), `pyproject.toml` `[tool.coverage.report] exclude_lines` `"pass"`
Evidence: `exclude_lines` entries are regular expressions searched in each source line. `"pass"` matches every line containing the substring: `password`, `bypass`, `compass`, `passed`. Coverage excludes a whole block when the excluded line introduces it, so `if credential is None or not verify_password(...)`-type lines (service.py 265), `def hash_password`, `verify_password`, `reset_password`, `change_password(...)` definitions and bodies, `new_password` parameters etc. are excluded from the measured total.
Impact: reported coverage (80/85 gate) is inflated precisely on the most security-relevant code (auth/service.py). Inferred, verify with `coverage report` after a run, once with and once without the entry.
Fix: replace by `^\s*pass$`. Effort S. README: NEW. (Needs pyproject approval.)

### TST-02 high, tooling, `pyproject.toml` `[tool.coverage.run]`
`concurrency = ["thread"]` but the app runs async with SQLAlchemy greenlets; coverage needs `concurrency = ["thread", "greenlet"]` (and `multiprocessing` if xdist is used) or lines executed in greenlets/async frames can be reported as missed. `omit` removes `src/main.py` (middleware order, handlers registration, lifespan) and `src/common/logging.py` from coverage; `source` and `include` duplicate each other; `parallel=false` blocks xdist coverage combine. Effort S. NEW.

### TST-03 medium, tooling, `pyproject.toml` pytest addopts vs coverage fail_under (Known issue 9 CONFIRMED)
`--cov-fail-under=80` in addopts vs `fail_under = 85`; the CLI flag wins, so the effective gate is 80 and 85 is dead. Branch coverage is on (`branch = true`), fine. Fix: single value in `[tool.coverage.report]`, remove the CLI flag, target 85 initially then raise per phase. Never lower. Effort S.

### TST-04 high, flakiness/robustness, `tests/conftest.py:26-50` (`pytest_configure` starts containers)
Evidence: containers start in `pytest_configure`, i.e. on any invocation including `pytest --collect-only`, `pytest --help`-adjacent runs, IDE discovery, and in every xdist worker (each worker would start its own pair and race on env vars). Also `pytest_unconfigure` stops containers but a crash before it leaks them (no Ryuk reliance noted). Containers are Postgres 15 while README roadmap targets Postgres 18 (version drift between test and prod: UUIDv7 and other 18 features untestable).
Impact: slow discovery, cannot run unit tests without Docker, no parallelism.
Fix: session-scoped fixtures with `PostgresContainer` as context manager; env set in a fixture BEFORE `src` imports (settings are a module global, see TST-07); lazy so that unit tests do not need Docker. Pin image to the production major (18). Effort M.

### TST-05 high, bug, `tests/conftest.py:37-42` Redis wiring (Known issue 1 CONFIRMED and sharpened)
Evidence: conftest exports `REDIS_URL`; `src/common/config.py:32-35` reads `CACHE_HOST`, `CACHE_PORT`, `CACHE_DB`, `CACHE_PASSWORD`. `.env.unittest` defines none of them. So tests hit `localhost:6379` DB 0 and the started Redis container is dead weight (about 2-5 s startup wasted). Worse: tests share a developer's real local Redis (refresh-token keys `rt:*` and cashews route cache), `clean_db` never flushes Redis, so keys persist across tests and runs.
Impact: non-isolated, can corrupt a dev Redis, fails on CI without a sidecar. Fix (test side, no src change): set `CACHE_HOST`/`CACHE_PORT` from the container; add a `FLUSHDB` in the per-test cleanup. Effort S. Not a handover to backend-engineer unless `REDIS_URL` should become the real setting (Known issue 10).

### TST-06 high, bug, `tests/conftest.py:58-81` double schema creation (Known issue 2 CONFIRMED)
`alembic upgrade head` followed by `create_all` (a no-op on existing tables but masks migration drift: a model column missing from migrations is silently created by `create_all`) and `drop_all` at the end. Impact: migrations are never verified against models. Fix: only `alembic upgrade head`; add a test asserting `alembic check` (autogenerate diff is empty) and `upgrade head` / `downgrade base` round trip. Note `alembic.ini` is opened by relative path, so tests only work from `backend/` cwd. Effort S.

### TST-07 medium, flakiness (inferred), `tests/conftest.py:70-124` event loops
- `asyncio.run()` is called in `init_db` and in every `clean_db` teardown while the application's module-global `AsyncEngine` (`src/common/database.py`) is also used by `TestClient`'s anyio portal loop. Pooled connections created in loop A are reused in loop B. With asyncpg that raises "attached to a different loop"; here the DSN is rewritten to `postgresql+psycopg://` (conftest 33-35) although AGENTS.md/README say asyncpg, so the suite tests a different driver from production (psycopg is in dependencies only for this). Driver difference is a production-parity gap (NEW).
- `asyncio.set_event_loop_policy(WindowsSelectorEventLoopPolicy())` at import: deprecated direction in 3.14 (policy system deprecated, removal in 3.16); needed for psycopg async on Windows only. Inferred: emits DeprecationWarning, currently hidden by `ignore::DeprecationWarning` in filterwarnings, which also hides every other deprecation.
- Fix: `engine.dispose()` after each loop use, or better do cleanup through a sync psycopg connection to the container (no loop at all), or move to an async test architecture (section 5). Effort M.

### TST-08 medium, gap/speed, `tests/conftest.py:95-100` function-scoped `TestClient(app)` with lifespan
Every test runs full lifespan (Redis ping, pool init, taskiq broker startup, DB init) and a fresh portal thread, plus a TRUNCATE of 5 tables afterwards (about 5-20 ms each). Argon2 default parameters hash on register/login/change: a normal suite spends most of its time in Argon2 (~50-100 ms per hash). Fix: pass cheap Argon2 parameters via a test-only settings override (needs a production seam: `PasswordHasher` is a module global `_ph` in service.py:44, can be monkeypatched in tests to `PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)`, no src edit). Effort S. Inferred; measure once Docker is available.

### TST-09 medium, best-practice, isolation: truncate after, not before
`clean_db` truncates in teardown only. If a test run is killed, or a failing fixture setup precedes it, the next test sees dirty rows. Better: truncate before (or both), per-test via a connection that is not the app engine. Truncate list is hard-coded (`auth_audit_log, email_tokens, sessions, password_credentials, users`) and silently goes stale when a table is added; derive from `Base.metadata.sorted_tables`. Transaction-rollback-per-test is NOT used and is not compatible with the app committing inside crud (Known: crud commits, Phase 4 unit-of-work). Keep truncate until Phase 4. Effort S.

### TST-10 high, gap, determinism: no time control anywhere
`utcnow()` / `datetime.now(timezone.utc)` called directly in service, models, crud, jwt_utils. Consequences: no test for access-token expiry (AGENTS.md demands "expired token" for every endpoint; only missing and invalid are tested), lock expiry (`locked_until`), email-token expiry, refresh-session expiry, Redis TTL. Fix without src change: `time-machine` (see section 4) patches `datetime.now`/`time.time` in C code paths used by PyJWT and SQLAlchemy defaults; Postgres `now()` server defaults are not affected, so expiry must be set through crud (direct UPDATE of `expires_at` in the DB) for DB-side checks. Effort M. NEW.

### TST-11 medium, gap, captured_emails (`conftest.py:141-157`)
Patches `.kiq` of three task objects at their import location inside `service` modules. Weaknesses: a new importer (`from ... import send_x_task` in another module) is silently not patched, real broker would be used; captures only the LAST token per address (cannot assert "two mails sent", "no mail sent for unknown email", or count) and drops `username`; subject/template/link are never asserted. Fix: capture list of `(task, to, username, token)`, patch the single source (`src.utils.email_tasks.<task>.kiq`) and also assert that no task is enqueued on unknown email. Template rendering test through fastapi-mail `fm.record_messages()` instead of `.kiq` mock. Effort S.

### TST-12 high, gap, refresh-token security (see section 3)
Code facts supporting the gaps: `service.py:371-374` Redis `GET` then `DELETE` (two round trips, not atomic) so two concurrent refreshes with the same cookie can both succeed and fork a family; `service.py:377-392` fast path ignores whether the DB session is already revoked, expired, or whether the user is deactivated/locked (a deactivated user keeps minting tokens until the refresh token expires, 30 days); `service.py:395-416` when Redis lost the key (eviction/flush/restart) a valid, unrevoked session returns "Invalid" (silent logout of everyone on Redis restart, availability) and an expired DB session is not checked. Hand to backend-engineer: use atomic `GETDEL`, check DB state as source of truth. Effort M.

### TST-13 medium, bug (testable), `src/utils/jwt_utils.py:56-58`
`user_id = str(payload.get("sub"))` turns a missing claim into the string `"None"`, so the later `is None` check can never trigger. Test: forge a token signed with the correct key lacking `sub`/`jti`; expected 401, actual: `uuid.UUID("None")` raises ValueError in `get_current_user` -> 500 (inferred) rather than 401. Also no `type=access` check: a refresh/other-purpose JWT with the same key would be accepted; alg `none` and wrong-alg tests missing. Write failing regression test first, hand fix to backend-engineer. NEW.

### TST-14 medium, gap, `tests/metrics/test_metrics.py`
Two tests; assert only `# HELP` present. No test of counters incrementing, label cardinality (route template vs raw path: `middleware.py:169` returns `object` under ty), exposure without auth (README Phase 1 "protect /metrics"), Known issues 3 and 4 not pinned (tests that fail today: `/metrics/` self-counting, `active_refresh_tokens` stays 0 after login). These should be written as strict tests and marked `xfail(strict=True, reason="Known issue 3")` only with an issue reference per boundaries.

### TST-15 low, best-practice, pyproject markers and warnings
Markers (`auth`, `users`, `unit`, `root`) are declared but not applied; `--strict-markers` and `--strict-config` absent; `filterwarnings` ignores all `DeprecationWarning` globally (hides Python 3.14/SQLAlchemy/Starlette deprecations that become errors in upgrades). Target: `error` plus narrow, commented ignores. `pytest-asyncio` 1.3.0 is installed (lock) but unused and no `asyncio_mode` configured. `pytest`, `pytest-cov`, `httpx`, `testcontainers`, `mypy` sit in production `[project.dependencies]` (Phase 0 item) and `[tool.pytest.ini_options]` is the legacy table (native `[tool.pytest]` exists since 9.0). Effort S.

### TST-16 medium, gap, test architecture
No unit tests; no Hypothesis; no contract test; no helper module (helpers `register`/`login`/`bearer` are re-declared per test file: `tests/auth/test_auth.py` and `tests/user/test_user.py` each duplicate them). No `tests/` mirror for `src/common` (middleware, exceptions, responses, config, cache) which hold the cross-cutting security behaviour. Effort M.

### TST-17 low, flakiness (inferred), Windows specifics
`WindowsSelectorEventLoopPolicy` plus Docker Desktop port mapping `localhost` vs IPv6 `::1` (Redis URL uses `localhost`); prefer `container.get_container_host_ip()`. Testcontainers wait strategy for Postgres is log-based; fine. `OneDrive`-synced working dir with `.venv`, `.mypy_cache`, `htmlcov` inside is a performance and file-lock risk (S, NEW, environment note).

### TST-18 medium, gap, error envelope and 500 handler
`generic_exception_handler` is registered (`main.py:129`) but no test asserts that an unhandled exception yields a generic 500 body without type/stack/SQL, with `X-Request-ID` and security headers present, and error bodies on 401/403/404/405/422 follow one shape. Starlette's `ServerErrorMiddleware` re-raises for `TestClient(raise_server_exceptions=True)` default: test needs `TestClient(app, raise_server_exceptions=False)`. Validation handler echoes input? Needs a test that a 422 body does not reflect the submitted password (Pydantic `input` field leaks the value by default). Inferred risk: high for register/login/password change bodies. Effort S.

## 3. Missing test cases, prioritised

P0 (security, cheap, do first)
1. Refresh: concurrent double-use of one refresh token (two threads via `ThreadPoolExecutor` or `asyncio.gather`): exactly one 200 (currently fails, race; TST-12). Marked as regression test, hand over.
2. Refresh reuse: after reuse detection the NEWEST token of the family is also dead (family revoke), audit row `SESSION_REVOKED` with detail; existing test only checks the old token.
3. Refresh with Redis flushed but DB session valid: define and pin behaviour (currently 401).
4. Refresh for deactivated/locked user: must be 401 and revoke (currently succeeds).
5. Refresh with expired session (time-machine +31 d) returns 401.
6. Access token expired (time-machine +16 min) on `/user/me`, `/user/me` PATCH, change-password, resend: 401 each. Token with `nbf` in the future, wrong signature, alg `none`, HS384 with the same key, missing `sub`/`jti` (TST-13), token for deleted user, token of deactivated user mid-session.
7. Refresh cookie attributes: HttpOnly, Path=/auth, SameSite, `Secure` absent (document Known issue 8 with a failing strict test when Phase 1 lands), Max-Age/expires present, deleted on logout (`Max-Age=0`), cookie not sent to other paths.
8. Password reset: expired token (time-machine +1 h, 1 s), token of purpose VERIFY_EMAIL rejected at reset (purpose confusion), token hash only stored (DB column not equal raw token), reset does not unlock/verify as side effect, old sessions still valid (pins Known issue 5 as failing strict test), reset for deactivated user, audit rows `PASSWORD_RESET_REQUESTED/COMPLETED`, mail count exactly 1.
9. Email verification expired token; reuse; VERIFY token used at reset endpoint; resend rate (no limit exists, Phase 1).
10. Login lockout: lock expiry (time-machine past `locked_until` then correct password succeeds, `ACCOUNT_UNLOCKED` audit row, counter reset); locked account with CORRECT password still rejected; counter at exactly MAXIMUM_LOGIN_ATTEMPTS-1 then success resets; username enumeration: response body and status for unknown user vs locked vs inactive vs wrong password (currently 4 different messages, pins Phase 1 uniform-error item); response time not asserted.
11. Error envelope: 401 without `WWW-Authenticate: Bearer`, 404 unknown path, 405, 422 body does not echo password, 500 handler generic (TST-18), `X-Request-ID` honoured/generated and reflected, oversized `X-Request-ID` handling (log injection, CRLF), security headers on error responses.
12. Mass assignment: `PATCH /user/me` with `is_active`, `is_email_verified`, `roles`, `id`, `email_verified` fields (currently ignored? must be 422 once `extra="forbid"` lands); register with `is_active`/`is_email_verified`. Oversized: username/email/password/display_name beyond max, 1 MB body, unicode normalisation (homoglyph username duplicates), NUL byte in strings (Postgres 500!), email case variants for duplicate check.
13. BOLA with two users: user B cannot read, update or revoke user A's sessions/tokens (token reuse across users); B's refresh cookie cannot be used with A's access token; email token of A used by B's request.

P1
14. Email change: PATCH email creates CHANGE_EMAIL token with `new_email`; token is not consumable at `/auth/email/verify` to change address (Known issue 6: no consumer); duplicate email race (two users change to the same address, DB unique constraint -> 409 not 500); old email unchanged; second change invalidates first token; audit row missing today (`update_profile` writes none: gap NEW, no `_audit` call in `user/service.py:35-75`).
15. Register: case-insensitive duplicate username (`Alice` vs `alice`), concurrent duplicate registration (unique constraint race -> 409, not 500; `register_user` does check-then-insert, TOCTOU), email send failure (kiq raises) -> user created? (non-atomic, Phase 4 item), audit rows REGISTER + EMAIL_VERIFICATION_SENT counts.
16. Logout: unknown/other user's token, double logout (second returns 401 "Session not found" while cookie cleared?), logout clears the Redis key (assert via Redis client), logout does not revoke other sessions of the user.
17. Cache invalidation: `GET /user/me` route cache (cashews, `ROUTE_CACHE_TTL` 2m) after PATCH shows fresh data and not another user's data (key must include user); currently the route cache design is untested. HIGH if the cache key lacks user identity: verify.
18. Password change: current password equal to new, change does not revoke sessions (Known issue 5 pin), audit `PASSWORD_CHANGED`, 5 wrong current passwords do not lock (no throttle: pins Phase 1 gap).
19. Config/startup guard: default `JWT_SECRET_KEY` in prod environment refuses to boot (once Phase 1 lands); `Settings` unit tests per environment.
20. Alembic: `upgrade head`, `downgrade base`, `upgrade head` round trip; `alembic check`; constraint tests directly against DB (unique lower(email), FK cascade on user delete, enum values).
21. Metrics: counters/histogram updated after a request, label uses route template not raw path (no user id in label), `/metrics` self-exclusion (Known issue 4 strict test), gauge `active_refresh_tokens` > 0 after login (Known issue 3 strict test).

P2
22. Schemathesis stateful run against `/openapi.json` (all 4xx schema-conformant, no 5xx); Hypothesis for username/email/password validators and `hash_token`, `decode_token` (never raises, never returns data for random strings).
23. Mutation testing on `auth/service.py` and `jwt_utils.py`.
24. Concurrency/load smoke: 50 parallel logins on one account lock correctly and never lose the failed-attempt count (read-modify-write `user.failed_login_attempts += 1` at service.py:267 is a lost-update race; brute-force bypass: N parallel wrong guesses count as fewer attempts). Test with `asyncio.gather` on ASGI client; hand to backend-engineer (atomic UPDATE ... RETURNING).

## 4. Tooling evaluation (2026-11-16)

Versions installed (uv.lock): pytest 9.0.2, pytest-asyncio 1.3.0, anyio 4.12.1, httpx 0.28.1, testcontainers 4.14.1. Latest-version figures come from the agent brief (verified 2026-11-12, not re-verified today): pytest 9.2.0, pytest-asyncio 1.4.1, testcontainers 4.15.0, Hypothesis 6.170, Schemathesis 4.33, polyfactory 3.5. time-machine, pytest-xdist, asgi-lifespan, fakeredis versions NOT verified (no web check performed in this run; verify before proposing a lock change).

| Topic | Recommendation | Reason / trade-off |
|---|---|---|
| Async plugin | pytest-asyncio (already in lock, 1.x) with `asyncio_mode = "auto"`, `asyncio_default_fixture_loop_scope = "session"`, `asyncio_default_test_loop_scope = "session"` | One loop for the whole session solves the engine-in-multiple-loops problem (TST-07) because the app's module-global engine, Redis pool and the tests share a loop. anyio plugin is an equal alternative and is already pulled in by Starlette, but fixtures with session loop scope are better documented in pytest-asyncio; do not use both. Unset loop scope + `filterwarnings=error` would break (brief). |
| HTTP client | `httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")` | No portal thread, same loop as DB/Redis fixtures, real concurrency tests (races, TST-12/24) possible with `asyncio.gather`; TestClient serialises through a thread and cannot test races. ASGITransport does NOT run lifespan. |
| Lifespan | `asgi-lifespan` `LifespanManager(app)` once per session (or the Starlette-native lifespan call), not per test | Per-test lifespan is a main cost (TST-08). Unverified: current asgi-lifespan release and Python 3.14 compatibility; alternative is to invoke `app.router.lifespan_context(app)` in a session fixture, zero extra dependency. Prefer that. |
| Containers | session-scoped fixtures, Postgres 18 image matching prod, Redis 7 wired via `CACHE_HOST/PORT`; `wait_for_logs` defaults; no container start in `pytest_configure` | See TST-04/05. |
| xdist | Optional later. With a session container per worker (`gw0..gwN` own DB: `CREATE DATABASE test_gw0` inside one shared Postgres container started by the controller via `pytest_configure_node`/file lock) | Only useful when suite > ~60 s. Argon2 cheapening (TST-08) gives more for less. Requires `coverage` `parallel = true` + `concurrency` incl. greenlet. Defer; not first step. |
| Isolation | TRUNCATE ... RESTART IDENTITY CASCADE before each test via a separate sync/async admin connection; Redis `FLUSHDB` | Rollback-per-test impossible while crud commits (Phase 4). |
| Time | `time-machine` (C-level patch, works with PyJWT, datetime.now, time.time; supports `tick=False` and `travel(..., tick=True)`) over freezegun | Faster and covers C callers. Limits: Postgres `now()` and Redis server TTL are NOT moved; for Redis expiry use `redis.expire(key, 1)`-style direct manipulation or assert TTL value (`TTL rt:<hash>` <= 30 d). Better long-term: production clock seam (`Clock` dependency) via backend-engineer. Python 3.14 support of time-machine unverified; check before adding. |
| Factories | polyfactory 3.x not needed now (4 models, one `RegisterRequest`). Use a small hand `make_user()` helper in `tests/helpers.py`. Revisit when schemas grow. | Avoid dependency for 20 lines. |
| Property tests | Hypothesis (dev group) for validators and token helpers only; `deadline=None`, `derandomize` in CI profile | Large input space (unicode, lengths), pure functions. |
| Contract | Schemathesis 4.x: `schemathesis run` against ASGI app in-process (`schemathesis.openapi.from_asgi`) with stateful links, restricted to checks `not_a_server_error`, `status_code_conformance`, `response_schema_conformance`; run in nightly CI, not in the PR gate | Finds 500s (NUL bytes, unicode) that example tests miss. Will fail today until the response models (Phase 2) are complete; start with `--checks not_a_server_error` only. |
| Mutation | mutmut on `src/api/auth/service.py`, `src/utils/jwt_utils.py` nightly; require pytest invocation limited to `tests/auth`, `tests/user` | Measures assertion strength; the TST-01 coverage hole shows line coverage alone misleads. Windows: mutmut needs WSL/Linux (unverified); run in CI Linux only. |
| Coverage | branch, single threshold 85 in `[tool.coverage.report]`, fix TST-01/02, report XML for CI, `--cov-branch` | |
| Config | native `[tool.pytest]` table (9.0+), `strict_markers`, `strict_config`, `filterwarnings = ["error", ...narrow ignores]`, `minversion = "9.0"` | |

## 5. Target test architecture

```
tests/
  conftest.py            # session fixtures only: containers, env, settings, migrated DB, app+lifespan, engine
  helpers.py             # register/login/bearer/make_user, Redis + DB inspectors, audit-row query
  unit/                  # no Docker: jwt_utils, hash_token, schemas validators, config guards, middleware pure funcs (Hypothesis)
  integration/
    auth/ user/ metrics/ root/ common/ (middleware, exceptions envelope, headers, cache)
  db/                    # alembic round trip, alembic check, constraints
  contract/              # schemathesis (nightly marker)
```
Fixtures:
- `postgres_container` / `redis_container` (session, lazy, Docker-skip with a clear message when daemon missing, never `skip` silently in CI).
- `settings_env` (session, autouse, runs before first `src` import): DATABASE_URL (same driver as prod), CACHE_HOST/PORT, JWT secret random per session, cheap Argon2.
- `migrated_db` (session): `alembic upgrade head` only.
- `app` (session, via lifespan context), `client` (function): `AsyncClient(ASGITransport(app))` fresh cookie jar per test.
- `_clean` (function, autouse): TRUNCATE (derived from metadata) + FLUSHDB before test.
- `emails` (function): list-capturing fake at `src.utils.email_tasks` boundary.
- `clock` (function): `time_machine.travel` wrapper.
- `pytest-asyncio`: auto mode, session loop scope for fixtures and tests.
Markers: `unit`, `integration`, `contract`, `slow` with `--strict-markers`; PR gate = unit + integration; nightly = contract + mutation.
Speed budget: unit < 5 s; integration < 60 s without xdist.

## 6. D6 recommendation (type checker)

Measured: `mypy src` = 6 errors in 1 file (admin.py, one pattern: `UUID(form value)`); config is NOT strict yet (`pyproject.toml` has only overrides ignoring alembic/sqlalchemy/redis imports; sqlalchemy is ignored which hides most ORM typing errors, remove after verifying the SQLAlchemy mypy plugin status: the plugin is deprecated for 2.0 style, native `Mapped[]` typing works without it). `ty check src` = 15 diagnostics (pre-release; sample showed `middleware.py:169` returning `object`). `pyrightconfig.json` exists in the repo root, so pyright is already in editor use (Pylance), no CI run of it was done here.
Recommendation: keep the README lean (mypy strict as the CI gate, `ty` advisory non-blocking). Reasons: mypy is already installed, runs clean-ish (6 errors, trivially fixable), is the most mature with pydantic/SQLAlchemy plugins; `ty` is still beta and its diagnostics will move, so it must not gate. Run pyright only in the editor (pyrightconfig.json exists) rather than adding a third gate. Re-evaluate `ty` as gate when it reaches stable. Prerequisites: fix 6 admin.py errors (`str(form["id"])` with isinstance guard), then enable `strict = true` incrementally (module-by-module overrides) and delete the `sqlalchemy.*` and `redis.*` ignore_missing_imports (both ship types; `# type: ignore` on `redis.asyncio` imports is then removable). Also ty should run on tests with a looser profile. Effort M. This is not verified against a pyright run; the choice is a trade-off, not a measurement of pyright.

## 7. Handover to backend-engineer (tests I could write that need production change)

- Atomic refresh rotation (`GETDEL`), DB as source of truth for expiry/revoked/user state (TST-12).
- `decode_token` claim validation, `type=access`, no `str(None)` (TST-13).
- Lost-update on `failed_login_attempts` (P2-24).
- Test seams: injectable clock; `PasswordHasher` parameters from Settings; `REDIS_URL` vs `CACHE_*` settings (Known issue 10).
- Audit event missing on profile/email change.

## 8. Known issues status

| # | Status |
|---|---|
| 1 | CONFIRMED (sharpened: also Redis never flushed, TST-05) |
| 2 | CONFIRMED (TST-06; the loop part inferred TST-07) |
| 9 | CONFIRMED (TST-03) |
| 14 | NOT REPRODUCIBLE here: venv works with Python 3.14.4, mypy and ruff run. Likely machine-specific or already repaired; recommend marking as fixed/resolved after the user confirms on their machine. |
| 13 | not checked |
| NEW | TST-01, 02, 04, 07 (driver mismatch), 10, 11, 13, 17, 18, mypy red (6 errors, README implies "exit 0" is a Phase 0 goal, fine) |

## 9. Open questions for the user (needed before writing tests)
1. Docker Desktop must be started to measure baseline (pass/fail, coverage, duration); may I run the suite once it is?
2. Approve pyproject changes: coverage regex fix and threshold, native `[tool.pytest]`, dev dependency groups (time-machine, hypothesis, schemathesis as dev-only).
3. May the Redis wiring and the Postgres image/driver parity (psycopg vs asyncpg, 15 vs 18) be fixed in conftest.py as part of this task?
4. Is the async test architecture (httpx ASGITransport, session loop) acceptable as a replacement for TestClient (rewrites about 1100 LOC of helper calls mechanically)?
