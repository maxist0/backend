# Researcher report (condensed, accessed 2026-11-16)

## Versions locked → latest
fastapi 0.136.1→0.163.1 | starlette 0.52.1→1.10.0 | pydantic 2.13.3→2.14.3 | pydantic-settings 2.14.0→2.16.3 | uvicorn 0.40.0→0.54.0 | sqlalchemy 2.0.49→2.1.8 (2.1.0 final 2026-09-24) | asyncpg 0.31→0.32 | psycopg 3.3.3→3.3.9 | alembic 1.18.4→1.20.1 | redis-py 7.4→8.1 (major) | pyjwt 2.11.0→2.15.1 | argon2-cffi 25.1.0 current | structlog 25.5→26.1 | prometheus-client 0.24.1→0.26 | otel 1.39.1→1.45.2 (LoggingHandler deprecated since 1.40) | fastapi-mail 1.6.2→1.6.8 (single maintainer) | taskiq 0.12.2→0.13.0 | taskiq-redis 1.2.2→1.2.4 | cashews 7.5→7.6 | sqladmin 0.25→0.32 (SA 2.1 compat unverified) | python-multipart 0.0.22→0.0.33 | testcontainers 4.14.1→4.15 | pytest-asyncio 1.3→1.5 | ruff 0.15.0→0.17.5 | mypy 1.19.1→2.4.1 (major) | ty 0.0.104 beta | uv 0.13.8.
requires-python says >=3.13 (docs 3.14). pytest in main deps.

## Advisories on locked versions (verified GitHub)
- PyJWT 2.11.0: GHSA-ffc3-869f-jxw9 critical (alg confusion, only with mixed HMAC+asym allow-list — NOT exploitable here: single alg list, checked jwt_utils.py:54); GHSA-9j54 (empty HMAC JWK, n/a); GHSA-gvp8 options-dict mutation (fix unverified); GHSA-hxm8 non-canonical signature bypasses raw-token revocation; several more unread. Fix: >=2.15.1.
- Starlette 0.52.1: GHSA-82w8 urlencoded form DoS (<1.3.1, High); GHSA-86qp Host header poisons request.url.path (<=1.0.0); GHSA-x746 (<1.1.0).
- python-multipart 0.0.22: GHSA-pp6c (<0.0.27 High), GHSA-5rvq (<0.0.30 High), GHSA-mj87 (<0.0.26), more. Fix >=0.0.33.
- Not checked: cryptography, redis, structlog, taskiq, sqladmin, cashews, fastapi-mail, anyio → run uv audit/pip-audit.

## FastAPI 0.136→0.163 relevant
0.137 router tree (router.routes changed; iter_route_contexts) | 0.142 native OTel FastAPI(telemetry=) | 0.143 exporters opt-in | 0.145 removes openapi_prefix/**extra | 0.146 root_path deprecated → fastapi run --root-path | 0.147–0.150,0.162 form handling changes (check OAuth2 form login) | 0.151 explicit JSON null ≠ omitted (422 on non-nullable) — frontend impact | 0.155 **extra removed from Param fns | 0.160 param model extra applies to siblings | 0.162.6 drops py3.10 | 0.163 Depends(max_concurrency=). Exists already: Depends(scope) 0.121, ORJSON deprecated 0.131, strict content-type 0.132, SSE 0.135, 3.14t 0.136. [tool.fastapi] entrypoint documented. No RFC 9457 in core.
Starlette 1.x: on_event etc. removed, Jinja autoescape default, anyio>=4,<5, TestClient streams (1.9).

## SQLAlchemy 2.1
psycopg3 default PG driver (asyncpg still supported), greenlet via [asyncio] extra, unconditional autoflush, loader option path change, filter_by ambiguity error, Select[int,str] typing, tstring().
Decision needed: one driver (repo has asyncpg + psycopg).

## Python 3.14
uuid.uuid7() stdlib; PEP 649 lazy annotations OK with pydantic; asyncio policies deprecated (removal 3.16) — tests use WindowsSelectorEventLoopPolicy; free-threading supported but optional. 3.15 released 2026-10-09; keep 3.14 target.

## Reference projects
full-stack-fastapi-template: SQLModel, uv, py>=3.14, pwdlib[argon2,bcrypt], pyjwt>=2.15, psycopg[binary], ruff>=0.16, mypy+ty. zhanymkanov layout matches repo. Netflix Dispatch archived 2025-09.
