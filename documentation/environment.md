# Environments and secrets

How the application knows which environment it runs in, where it reads configuration from, and how secrets are handled. Form: explanation with reference tables. The table of every setting, with defaults and bounds, is [README → Configuration](../README.md#configuration); this page does not repeat it.

## `ENVIRONMENT`

`ENVIRONMENT` is required. There is no fallback to `dev`: a missing, empty or unknown value stops the process with a message that lists the allowed values (`environment_from_os` in `src/app/core/config.py`). It is read from the process environment only, because it decides whether `.env.local` is read at all.

| Value | Set by | Docs and OpenAPI | Logs | `.env.local` | Startup guard | Admin panel |
|---|---|---|---|---|---|---|
| `dev` | `python run.py dev` | on | coloured console | read | off | on unless `ADMIN_ENABLED=false` |
| `unittest` | `tests/conftest.py`, `run.py unittest` | on | JSON | ignored | off | off unless `ADMIN_ENABLED=true` |
| `test` | the runtime | on | JSON | ignored | on | off unless `ADMIN_ENABLED=true` |
| `stage` | the runtime | on | JSON | ignored | on | off unless `ADMIN_ENABLED=true` |
| `prod` | the runtime | off | JSON | ignored | on | off unless `ADMIN_ENABLED=true` |

`python run.py` without an argument expects `ENVIRONMENT` from the runtime (Docker, Swarm). `run.py dev` and `run.py unittest` set it. `run.py dev` also starts the taskiq worker and scheduler.

## Where configuration comes from

Priority, highest first:

1. Environment variables, including those injected by Docker, Swarm or CI.
2. `.env.local`, only with `ENVIRONMENT=dev`. The file is git-ignored and relative to the working directory (`backend/`). A missing file is skipped.
3. The defaults in `Settings`.

`.env` is a tracked reference of every variable. Each active line holds the `Settings` default; secrets, settings that are unset by default, `ENVIRONMENT` and `APP_VERSION` are commented out. It is **not read** by the application. Copied to `.env.local` unedited it changes nothing (`tests/unit/core/test_env_template.py`), so `cp .env .env.local` is a working start; then change the values you need.

`get_settings()` builds the settings once per process (cached). Unknown variables are ignored. Validation errors never echo input values, because they can be secrets. Alembic does not build `Settings`: it reads `DATABASE_URL` only ([database.md](database.md#migrations)).

## Startup guard

Outside `dev` and `unittest` the application refuses to start when:

- `JWT_SECRET_KEY` is the public default or shorter than 32 characters;
- `FRONTEND_URL` is the development default (`http://localhost:5173`) or does not use `https`, because email links carry tokens;
- the admin panel is enabled and `ADMIN_PASSWORD` or `ADMIN_SECRET_KEY` is a public default or shorter than 32 characters.

The error names the variables, not their values. Independent of the environment, the validators also refuse `CORS_ORIGINS` entries that are `*` or carry a path, a `DATABASE_URL` with another driver than psycopg, and Argon2 costs below the OWASP minimum.

## Secrets

| Secret | Variable | Notes |
|---|---|---|
| JWT signing key | `JWT_SECRET_KEY` | 256 bits of randomness. The algorithm is HS256, pinned in code. Changing it invalidates every access token already issued |
| Admin password and session key | `ADMIN_PASSWORD`, `ADMIN_SECRET_KEY` | only checked when the admin panel is enabled |
| Database | `DATABASE_URL` (contains the password) | the password is masked in logs |
| Redis | `CACHE_PASSWORD`, and the passwords inside `TASKIQ_BROKER_URL` and `TASKIQ_RESULT_BACKEND_URL` | |
| SMTP | `MAIL_PASSWORD` | without the full mail configuration, mail tasks send nothing |

- Generate a key with `python -c "import secrets; print(secrets.token_urlsafe(48))"` (the command from the `.env` template).
- Secrets never enter git: `.env.local` is ignored, and `.env` holds none.
- Inject secrets as environment variables at runtime. `docker stack deploy` does not read `.env`: export `JWT_SECRET_KEY` (and `DB_PASSWORD` and `REDIS_PASSWORD`, whose compose fallbacks are development values) in the shell that deploys; the stack file fails the deploy when `JWT_SECRET_KEY` is missing. Docker secrets for all credentials are Roadmap Phase 6.
- Never print secrets: use `SecretStr` for a new secret and add it to the startup guard and the README table ([AGENTS.md](../AGENTS.md#security-rules)).

## Per environment

**Development.** `docker compose -f compose.dev.yaml up -d --wait`, then `run.py dev` ([README → Quick start](../README.md#quick-start)). The defaults match the compose file. Use a second compose project and other ports for experiments that must not touch your data: `DEV_POSTGRES_PORT` and `DEV_REDIS_PORT`, with matching `DATABASE_URL`, `CACHE_PORT` and `TASKIQ_*_URL`.

**Tests.** `tests/conftest.py` sets `ENVIRONMENT=unittest` and `ADMIN_ENABLED=true` and points every service address at a closed port. The integration fixtures then wire the Testcontainers addresses. See [testing.md](testing.md).

**Test and prod.** The runtime injects every variable. They differ from `dev` in the table above and from each other only in what you set: `prod` hides the API docs. See [docker.md](docker.md).

## Stage

The repository defines **no staging stack**: no staging compose file, no CI pipeline and no Kubernetes manifests exist (open decisions D5 and D11, Roadmap Phases 5 and 6). `ENVIRONMENT=stage` behaves like `test`: API docs on, JSON logs, `.env.local` ignored, the startup guard on and the admin panel off unless `ADMIN_ENABLED=true`. A staging deployment is therefore the production image with `ENVIRONMENT=stage` and staging values. The only code difference from `prod` is that the API docs stay on.

Checklist for a staging deployment:

1. Build the image and tag it ([docker.md](docker.md#image)).
2. Provide separate secrets and separate Postgres and Redis instances. Never point staging at production data or reuse production secrets.
3. Set `ENVIRONMENT=stage`, `JWT_SECRET_KEY`, `DATABASE_URL`, `CACHE_*`, `TASKIQ_*_URL`, an `https` `FRONTEND_URL`, `CORS_ORIGINS` for the staging frontend and `FORWARDED_ALLOW_IPS` for the proxy ([README → Configuration](../README.md#configuration)).
4. Run `alembic upgrade head` as a separate step before the API starts. Test the downgrade of a new migration on a copy first.
5. Start the API, a worker and **one** scheduler. Without the scheduler, retries and the nightly cleanup never run.
6. Check `GET /health/ready` and, for the worker, its metrics port ([monitoring.md](monitoring.md)). Send a real mail to confirm the `MAIL_*` values.
7. Do not route `/metrics`, `/health/ready` or `/admin` publicly.

Not covered yet: a staging data refresh or anonymisation job, a smoke-test suite against a deployed environment and a promotion workflow. They belong to the CI work in Roadmap Phase 5.
