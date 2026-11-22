# Testing

How the test suite is organised, how to run it and how to write tests for a module. Form: reference and how-to. The decision is [ADR 0007](adr/0007-test-split-unit-integration.md); the rules are in [AGENTS.md → Testing](../AGENTS.md#testing). A module's complete example tests are in [modules.md](modules.md#step-12-the-tests).

## Two suites

| Suite | Folder | Needs | Rule |
|---|---|---|---|
| Unit | `tests/unit/{core,modules/<name>}` | nothing | The code under test needs no database and no Redis |
| Integration | `tests/integration/{core,contract,modules/<name>,admin}` | a running Docker daemon | `TestClient` against real Postgres and Redis (Testcontainers) |

`tests/conftest.py` marks every test `unit` or `integration` by its folder and stops the run for a test outside both folders. There is no backend end-to-end folder: frontend Playwright covers the journeys. Do not test third-party libraries; guard our own configuration of them.

## Running tests

Run from `backend/`.

```bash
uv run pytest                                   # everything, with the coverage gate; needs Docker
uv run pytest tests/unit --no-cov               # fast loop, no Docker
uv run pytest tests/integration/modules/users/test_me.py::TestGetMe -x
uv run pytest tests/integration -k "refresh" -x
```

- `pyproject.toml` sets `addopts = "-v -ra --tb=short --cov=app --cov-report=term-missing"`. A unit-only run cannot reach the coverage threshold, so pass `--no-cov` for it.
- The threshold is `fail_under` in `[tool.coverage.report]` (branch coverage; the value is in `pyproject.toml`). It applies to the full run. Do not lower it to make a change pass.
- To prove the unit suite needs no Docker: `DOCKER_HOST=tcp://127.0.0.1:1 uv run pytest tests/unit --no-cov` (bash). On 2026-11-22 this ran green.
- Warnings are errors: `filterwarnings` turns `DeprecationWarning`, `PendingDeprecationWarning`, `RuntimeWarning` and the FastAPI and Starlette deprecation warnings into failures, with one documented exception. Fix a new warning or list it in `pyproject.toml` with a reason and an exit condition.
- Test files are named for the behaviour they cover, not for a review finding.

## Environment of the tests

`tests/conftest.py` runs before the first `app` import:

- It sets `ENVIRONMENT=unittest` and `ADMIN_ENABLED=true`. `unittest` never reads `.env.local`.
- It points `DATABASE_URL`, `CACHE_HOST`/`CACHE_PORT` and the `TASKIQ_*_URL` values at a closed port (`127.0.0.1:1`), so nothing imported early can reach your own Postgres or Redis. Unit tests keep these addresses for the whole run.

The integration fixtures start the containers once per session and wire the real addresses:

| Fixture | Scope | What it does |
|---|---|---|
| `postgres_container`, `redis_container` | session | Testcontainers with the images pinned in `tests/helpers.py` (the same as `compose.dev.yaml`; `test_dev_compose_parity.py` fails when they drift) |
| `test_environment` | session, autouse | points the settings at the containers, clears the settings cache |
| `sync_engine`, `migrated_db` | session | schema from **Alembic** (`upgrade head`, no `create_all`), as a deployment creates it |
| `clean_db`, `flush_redis` | function, autouse | `TRUNCATE` of every application table and `FLUSHALL`, **before** each test |
| `app`, `client` | function | a fresh app; a `TestClient` with its own cookie jar; tasks run in-process; a failed task fails the test |
| `redis_broker_client` | function | tasks stay in the Redis stream; no worker runs |
| `captured_emails` | function | replaces the mailer and records every sent mail |
| `db` | function | the `Db` SQL helper |

Unit fixtures: `settings_factory` builds `Settings` for a test (`unittest`, no admin; `settings_factory(APP_NAME="x")`) and `fresh_settings` clears the settings cache around a test.

Database tests use the sync `TestClient` and one sync psycopg engine for fixtures and assertions, never `asyncio.run` with the app's engine. On Windows, event loops are created through `LOOP_FACTORY` (a `SelectorEventLoop`) because psycopg cannot use the `ProactorEventLoop`.

## Tasks in tests

The `app` fixture replaces the broker with taskiq's `InMemoryBroker` plus the worker's database hooks. `kiq()` therefore runs the task before the request answers, against the test database, and `captured_emails` sees the mail. `run_task(client, task, *args)` runs a task outside a request. A task that raises fails the test at teardown. Tests of the Redis transport (stream entries, retry schedules, "no personal data in Redis") use `redis_broker_client`.

## Helpers

Use the helpers in `tests/helpers.py`; add a shared helper there instead of copying one into a test module.

| Group | Helpers |
|---|---|
| HTTP flows | `register`, `register_ok`, `login`, `register_and_login`, `bearer`, `login_tokens`, `refresh_with`, `change_password`, `change_email`, `verify_email`, `request_reset`, `reset_password` |
| Database | `Db` (`rows`, `one`, `scalar`, `execute`), `user_id_of`, `active_session_count`, `audit_count` |
| Mail | `CapturedEmails` (`to`, `of_kind`, `latest_token`) |
| Tasks | `run_task`, `call_in_app`, `run_async` |
| Logs | `log_events`, `log_text` |
| Redis | `redis_from_env`, `redis_keys`, `redis_dump`, `assert_nothing_personal_in_redis` |
| Admin | `admin_login`, `run_action` |
| Concurrency | `run_parallel` (threads released by a barrier) |
| Settings, tokens | `build_settings`, `forge_access_token`, `jwt_secret` |

## What each endpoint test covers

Every new endpoint gets a happy path, a validation error, an auth failure (missing, invalid, expired token), a not-found or conflict case and one test that proves the side effect (an audit row, an email, a cache invalidation). Every test must fail without the change it covers: break the code once and watch the test fail. Keep the suite deterministic: no sleeps, no real network; `time-machine` moves the clock where a test needs time.

## OpenAPI snapshot

`tests/unit/core/openapi_snapshot.json` is the committed API contract. The test compares the generated document with it exactly. After an intended contract change:

```bash
UPDATE_OPENAPI_SNAPSHOT=1 uv run pytest tests/unit/core/test_openapi_contract.py
```

Review the diff of the snapshot before you commit it.

## Tests that list module contents

`EXPECTED_TABLES` in `tests/unit/core/test_model_registry.py` and `TASK_NAMES` in `tests/unit/core/test_taskiq_and_logging.py` record the tables and the task names of the registered modules. A new module changes both ([modules.md](modules.md#step-13-update-the-core-tests-that-list-module-contents), README known issue 29).

## Continuous integration

There is no CI pipeline yet (open decision D5, Roadmap Phase 5). The gates in [README → Commands](../README.md#commands) are the checks a pipeline would run.
