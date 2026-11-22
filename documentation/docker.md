# Docker

The container image, the local development containers and the Swarm commands. Form: reference. Overview: [README → Deployment](../README.md#deployment). Open deployment work is listed in [README → Roadmap](../README.md#phase-6-deployment-hardening) (Phase 6) and in [known issues](../README.md#known-issues) 19 and 20.

## Development containers

`compose.dev.yaml` starts Postgres and Redis for local development and nothing else. It binds both to `127.0.0.1`, uses the public development credentials that match the `Settings` defaults, and pins both images by tag and digest (`tests/unit/core/test_dev_compose_parity.py` keeps the test containers on the same pins).

```bash
docker compose -f compose.dev.yaml up -d --wait     # start and wait for the healthchecks
docker compose -f compose.dev.yaml down             # stop, keep the data
docker compose -f compose.dev.yaml down -v          # stop and delete the data
```

A throwaway project with other ports leaves your own data alone:

```bash
DEV_POSTGRES_PORT=55433 DEV_REDIS_PORT=56380 docker compose -p scratch -f compose.dev.yaml up -d --wait
docker compose -p scratch -f compose.dev.yaml down -v
```

Never use this file or its credentials outside a developer machine.

## Image

```bash
docker build -t api:latest .
```

The `Dockerfile` builds in two stages on `python:3.14-slim`:

1. The builder installs uv, runs `uv sync --frozen --no-dev --no-install-project` (the dependency layer is cached), copies `src/` and runs `uv sync --frozen --no-dev --no-editable`, so the package and its metadata (`APP_VERSION`) are in the virtualenv.
2. The runtime copies the virtualenv, creates the non-root user `appuser`, copies the rest of the build context and runs as that user. `.dockerignore` excludes `.env*`, `.git`, `documentation/`, `deployment/`, `scripts/`, caches and build output; `tests/` is not excluded, so it is in the image.

The entry point is `python run.py`, so the container needs `ENVIRONMENT` in its environment. The port is 8000. The `HEALTHCHECK` calls `GET /health` every 30 seconds.

Checked on 2026-11-22 with a throwaway tag (removed afterwards): the image builds, `docker run --rm <image> --help` prints the launcher's help, the user is `appuser` (uid 999) and the installed package version is `1.0.0`.

A container gets its settings as environment variables. Outside `dev` and `unittest` the [startup guard](environment.md#startup-guard) needs a real `JWT_SECRET_KEY` and an `https` `FRONTEND_URL`. A full `docker run` against a database is untested here.

The image is not hardened yet: the base image is not pinned by digest, uv is installed with pip, and there are no build cache mounts or read-only root file system (Roadmap Phase 6).

## Swarm

`deployment/docker/api/docker-compose.yml` is the production stack: Traefik, Postgres, Redis, a one-shot `migrate` service, the API replicas, the taskiq workers, one scheduler and the taskiq dashboard. The workers run `taskiq worker app.worker:broker`, the scheduler `taskiq scheduler app.worker:scheduler --skip-first-run`, and the API the image's entry point. `deployment/` is git-ignored in this repository.

```bash
docker swarm init                                              # once
docker network create -d overlay --attachable monitoring
docker build -t api:latest .
docker stack deploy -c deployment/docker/api/docker-compose.yml --with-registry-auth prod
docker stack rm prod
```

These commands are **untested** here, and agents never run them. Export `JWT_SECRET_KEY` first: `docker stack deploy` does not read `.env`, and the stack fails the deploy without the value. The stack file does not set `FRONTEND_URL`, `CORS_ORIGINS` or `FORWARDED_ALLOW_IPS`; with `ENVIRONMENT=prod` the startup guard stops the API until `FRONTEND_URL` is a real `https` URL, and the cookie, CORS and proxy-header behaviour need the real values (deploy round).

Monitoring has its own stack in `deployment/docker/monitoring/` ([monitoring.md](monitoring.md)).
