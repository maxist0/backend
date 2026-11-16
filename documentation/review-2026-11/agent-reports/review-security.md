# Security audit (condensed) 2026-11-16 — overall HIGH (critical if Swarm stack deployed as is)
Verified by main session: SEC-01 (compose lacks JWT_SECRET_KEY/ADMIN_*), SEC-02 (Redis hit path skips is_revoked/is_active; revoke_family DB only), SEC-05 (database.py:88).

## Critical
- SEC-01 default secrets: config.py:50,79-81 defaults; compose api env lacks JWT_SECRET_KEY/ADMIN_*; /api/admin reachable via strip prefix; create_admin always mounted. PoC forged admin cookie + JWT. Fix: required SecretStr, startup guard, ADMIN_ENABLED=false, secrets in compose (deploy, out of scope but critical). Breaking for deployments.
## High
- SEC-02 refresh revocation ineffective: service.py:371-392 Redis hit rotates w/o DB is_revoked/is_active/locked; revoke_family DB-only; Redis keys raw token. Existing test test_auth.py:267-285 false assurance. Fix: Postgres atomic gate `UPDATE sessions ... WHERE token_hash AND NOT is_revoked AND expires_at>now() RETURNING`; drop Redis RT cache (or key by hash + denylist). M.
- SEC-03 non-atomic rotation (GET then DELETE) → two branches; root of frontend contract task 5. Fix via SEC-02 + reuse interval. S.
- SEC-04 pre-account-takeover chain: no verified check at login, reset/change don't revoke sessions (KI5), sessions survive. Fix: revoke all on reset/change/verify, invalidate tokens; optionally block unverified login. M.
- SEC-05 DB password logged database.py:88 + alembic/env.py:49 print. S. Rotate pw if shipped.
- SEC-06 login DoS: Argon2 sync on loop (40 ms, t3 m64MiB p4); urlencoded login body hits starlette/multipart advisories; no body limit/timeout/rate limit. Fix: anyio.to_thread + CapacityLimiter, upgrades, body/timeout limits. M.
## Medium
- SEC-07 lockout lost update (service.py:267,295) + window used as lock duration, no decay. Atomic UPDATE +1 RETURNING or Redis INCR; backoff instead of hard lock.
- SEC-08 enumeration: timing (no dummy hash on unknown/inactive), X-Process-Time header, distinct 409 register msgs, locked msg w/ seconds, forgot 500 for existing only, PATCH /user/me "Email already in use". Fix dummy verify, uniform 401, register 202 generic (frontend breaking), drop X-Process-Time in prod.
- SEC-09 admin actions do nothing: read form.getlist("pk") but sqladmin uses GET ?pks=; admin lock without locked_until auto-unlocks on next login; lock/deactivate don't end sessions; must_change never enforced.
- SEC-10 admin session: SessionMiddleware defaults (not Secure, Lax, 14d, stateless), CSRF via GET actions, open redirect via Referer, no admin audit/rate limit, == compare; sqladmin 0.25 advisories GHSA-54mc, GHSA-ccg5 (sort by password_hash). Upgrade 0.32.
- SEC-11 no absolute session lifetime (sliding 30d forever). Needs family_created_at migration.
- SEC-12 raw bearer secrets in Redis keys rt:<raw>, raw reset/verify tokens as taskiq args (Redis db1), 8-char RT prefix debug log.
- SEC-13 login CSRF (urlencoded simple request, no Origin check); fix Origin/Sec-Fetch-Site allowlist on POST /auth/*, cookie Secure/__Host-/Path=/ (contract task 4).
- SEC-14 metrics label cardinality (hasattr(request,"route") always False) — same as OBS-08.
- SEC-15 email change: no reauth, no rate limit, sends live token to any address w/ verify template, leaks "in use". Requirements for confirm endpoint listed.
- SEC-16 JWT: exp/sub/jti not required (token w/o exp never expires; sub→'None'), no iss/aud/typ, username in claims, alg from settings. Fix options require + at+jwt, pyjwt 2.15.1. Breaking: existing tokens invalid.
## Low
- SEC-17 UA >512 chars breaks audit insert after counter commit → no audit rows; X-Device-* >255 → 500; engine hide_parameters missing.
- SEC-18 email regex $ accepts trailing newline (duplicate account PoC); forgot email not validated; reset mail sent to request string not user.email. Use EmailStr.
- SEC-19 token single-use SELECT+UPDATE not atomic; pw change doesn't invalidate reset tokens; no notification mails; /user/me/password brute-force w/o counter; no breached-pw check.
- SEC-20 fastapi-mail Jinja autoescape off (latent).
- SEC-21 AGENTS.md cache pattern static key leaks across users if applied to auth'd GET (latent; docs fix).
- SEC-22 X-Request-ID unvalidated, X-Process-Time, /metrics/health shows pod/node.
## Known items: KI3,5,6,7,8,11 confirmed; trusted proxies understated (blocks D4); CORS hard-coded main.py:140-146; hard-coded roles/permissions placeholder service.py:82-83 risky. README claims to correct: reuse detection, "tokens only SHA-256", lockout, enumeration-safe forgot.
## Dependency audit (uv audit): starlette ≥1.3.1, python-multipart ≥0.0.31, sqladmin 0.32, pyjwt 2.15.1, aiosmtplib 5.1.3 (STARTTLS injection), cryptography ≥48.0.1, certifi 2026.10.18, pydantic-settings ≥2.14.2 (secrets_dir symlink), otel 1.45.2, anyio/urllib3/idna minor. Test tools in runtime deps.
## Decisions
- D2: keep sqladmin opt-in only (ADMIN_ENABLED=false except dev), internal network/SSO, upgrade, per-person admins with RBAC role, hardened session, Origin check, audit. Else remove.
- D3: small generic RBAC now (roles, permissions, role_permissions, user_roles, require_permissions dep deny-by-default); remove hard-coded placeholders; no perms in JWT unless perm_ver.
- D4: both — Traefik coarse per-IP; app `limits`+Redis per (account,IP), forgot per email/IP, email-sending per user, refresh per family; 429 Retry-After; trusted proxies first.
- D8: treat CRA/NIS2 deliverables as mandatory (SECURITY.md, SBOM, uv audit in CI, secure defaults, log minimisation).
- D12: keep argon2-cffi; params in Settings, check_needs_rehash, off-loop hashing.
## Open questions from auditor: EU market (CRA)? admin needed in prod? frontend+API same-site (SameSite=Strict)? .env.unittest not in .gitignore.
