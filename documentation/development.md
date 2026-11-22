# Development guide

Daily work on the backend: run it, change it, check it. Form: how-to. The first run is the [Quick start](../README.md#quick-start); the rules are in [AGENTS.md](../AGENTS.md).

## Run the stack

```bash
docker compose -f compose.dev.yaml up -d --wait        # Postgres 18 and Redis 7
ENVIRONMENT=dev uv run alembic upgrade head            # PowerShell: $env:ENVIRONMENT = "dev"; ...
uv run python run.py dev                               # API, worker and scheduler
```

- Alembic needs `DATABASE_URL` in `.env.local` (with `ENVIRONMENT=dev`) or in the environment ([environment.md](environment.md)).
- `run.py dev --port 8001` changes the port; `--host` and `--reload` exist too. `--reload` is untested with the worker and scheduler children.
- `fastapi dev` also works through `[tool.fastapi] entrypoint`; it needs `ENVIRONMENT` and starts no worker. Untested in this documentation pass.
- Stop the stack with Ctrl+C; stop the containers with `docker compose -f compose.dev.yaml down`.
- Swagger UI is at `http://localhost:8000/docs`: authorise with the password flow (the token URL follows `API_PREFIX`). The admin panel is at `http://localhost:8000/admin` (`admin` / `admin` in `dev`).
- Without a `MAIL_*` configuration the mail tasks send nothing and log `email.not_sent` (`reason=mail_not_configured`), so you cannot read a verification or reset token in `dev`. To try the mail flows, point `MAIL_*` at an SMTP server you run locally (untested), or use the integration tests, whose `captured_emails` fixture reads the tokens.

## Make a change

1. Read the module you touch and the rules for it in [AGENTS.md](../AGENTS.md).
2. For a new feature, follow [modules.md](modules.md).
3. Write the tests with the change ([testing.md](testing.md)).
4. Model change: autogenerate and review a migration ([database.md](database.md#migrations)).
5. New setting: `src/app/core/config.py`, the `.env` template and the README table. New secret: add it to the startup guard too.
6. Run the gates:

   ```bash
   uv run ruff check src tests run.py alembic
   uv run ruff format --check src tests run.py alembic
   uv run mypy
   uv run pyright
   uv run pytest
   ```

   `uv run ruff format src tests run.py alembic` fixes formatting. Pass explicit paths: ruff also formats Markdown code blocks, and not every Markdown file in `documentation/` is meant to be.
7. Update the README sections and the topic pages your change touches, in the same change.

Before a commit also run `uv audit --frozen` and `uv lock --check`. Commits follow Conventional Commits ([AGENTS.md → Git](../AGENTS.md#git-and-pull-requests)). There is no pre-commit configuration yet (Roadmap Phase 0).

## Editor

VS Code with Pylance reads `[tool.pyright]` in `pyproject.toml` (strict mode, `venvPath = "."`, `venv = ".venv"`), so select the interpreter in `.venv` and the editor and `uv run pyright` agree. `uv sync` creates `.venv`; never `pip install` into it by hand. If it is broken, run `uv sync --reinstall`.

## Debugging

- Read the log of one request by its `request_id` ([logging.md](logging.md)). `LOG_LEVEL=DEBUG` in `.env.local` raises the level of the app; taskiq's loggers stay at INFO because they would print task arguments.
- Look into the database: `docker compose -f compose.dev.yaml exec postgres psql -U postgres -d api`.
- Look into Redis: `docker compose -f compose.dev.yaml exec redis redis-cli` ([redis.md](redis.md#inspecting-redis-in-development)).
- A task that fails is logged by the worker with its exception type; a database error shows only the types and the SQLSTATE, by design.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `ENVIRONMENT is not set` | There is no default. Start with `python run.py dev`, or export `ENVIRONMENT` |
| `DATABASE_URL is not set` from Alembic | Alembic ignores `.env.local` unless `ENVIRONMENT=dev` is set; export `DATABASE_URL` or set `ENVIRONMENT=dev` |
| `Database connection failed` at startup | Postgres is not running or the URL is wrong: `docker compose -f compose.dev.yaml up -d --wait`, then check `DATABASE_URL` |
| The startup guard names variables | `ENVIRONMENT` is not `dev` or `unittest`, and a secret is a default or too short ([environment.md](environment.md#startup-guard)) |
| The app stops at import with "A title must be provided" | `APP_NAME` is empty, usually after copying `.env` to `.env.local` as is. Remove the line |
| `DATABASE_URL uses the asyncpg driver` | The driver is psycopg 3: use `postgresql+psycopg://` (or `postgresql://`) |
| `The database driver (psycopg) cannot run on Windows' ProactorEventLoop` | Start the API with `python run.py dev` or `fastapi dev`, and the worker with `python -m app.worker` |
| Port 8000, 5432 or 6379 is taken | `--port` for the API; `DEV_POSTGRES_PORT` and `DEV_REDIS_PORT` for the containers, with matching URLs in `.env.local` |
| A failed task is never retried | The scheduler is not running; `run.py dev` starts it ([redis.md](redis.md)) |
| Integration tests fail at fixture setup | The Docker daemon is not running; start Docker and retry |
| Imports or the editor look broken | `uv sync --reinstall`, then select `.venv` as the interpreter |
