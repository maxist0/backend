# Backend-Review 2026-11: konsolidierte Befunde und Umsetzungsplan

Stand: 2026-11-16 · Status: **Phase 0 + 0b umgesetzt (uncommitted), verifiziert: 223 Tests grün**; D1–D12 werden vor Phase 2/3 einzeln besprochen; entschieden: N1 = psycopg 3, N2 = keine Echtdaten → neue Baseline, N3 = Postgres 18 überall, D6 = mypy strict als Gate + ty advisory · Scope: App-Code und DB, Tests, Observability (Docker/Swarm/CI nicht im Scope, außer kritisch)

Sechs Agents haben das Backend nur gelesen und nichts geändert: `backend-engineer`, `db-engineer`, `security-auditor`, `backend-tester`, `observability-engineer` und `researcher`. Die Einzelberichte mit Datei:Zeile, Belegen und Fix-Skizzen liegen unter [agent-reports/](agent-reports/). Die Berichte `review-security.md` und `review-researcher.md` sind verdichtet, weil diese beiden Agents keine Dateien schreiben dürfen.

Insgesamt gab es rund 130 Einzelbefunde (SEC 22, BE 32, DB 30, OBS 28, TST 18). Nach dem Entfernen von Duplikaten bleiben die **44 Themen** unten. Die IDs in Klammern verweisen auf die Einzelberichte.

**Vom Hauptagenten selbst am Code nachgeprüft:**
- Refresh-Bypass: `service.py:371`
- Fehlende Secrets im Swarm-Stack: `docker-compose.yml`
- Klartext-Passwort im Log: `database.py:88`
- Leere Alembic-Metadaten: `alembic/env.py:27`
- Ein-Algorithmus-Liste in `jwt_utils.py:54`. Deshalb ist das kritische PyJWT-Advisory hier nicht ausnutzbar.

**Nicht ausgeführt:** `pytest`, weil kein Docker-Daemon lief. Alle Race-Conditions sind aus dem Code abgeleitet. Für jede ist ein Regressionstest geplant, der sie zuerst rot zeigen muss.

---

## 1. Gesamtbild

Das Fundament ist solide: async durchgängig, Argon2id, Tokens mit 256 Bit aus einem CSPRNG und als SHA-256 gespeichert, nur ORM und kein Roh-SQL, kein BOLA auf `/user/*`, eine saubere Modulstruktur pro Feature.

Mehrere Kontrollen, die das README als **umgesetzt** führt, wirken aber nicht:

| README-Aussage | Tatsächlich |
|---|---|
| Rotation mit Reuse-Detection und Family-Revocation | Wirkungslos. Der Redis-Fast-Path prüft Postgres nie, und die Rotation ist nicht atomar. |
| Postgres ist Source of Truth für Sessions | Redis ist faktisch die Autorität. Ein Redis-Flush meldet alle Nutzer ab. |
| Tokens nur als SHA-256 gespeichert | Roh-Refresh-Tokens sind Redis-Keys. Reset- und Verify-Tokens liegen als taskiq-Argumente in Redis. |
| Account-Lockout | Per Parallel-Requests umgehbar (Lost Update). Ein Admin-Lock hebt sich beim nächsten Login selbst auf. |
| Admin: Lock, Deactivate, Revoke | Die Aktionen tun nichts. Sie lesen `pk` aus dem Formular, sqladmin sendet aber `?pks=` per GET. |
| Alembic-Autogenerate | `Base.metadata` ist leer, der Autogenerate würde **alle Tabellen droppen**. |

Die Abhängigkeiten sind deutlich veraltet. Drei der gelockten Versionen haben veröffentlichte Advisories (Starlette, python-multipart, PyJWT), die über den Login erreichbar sind.

---

## 2. Befunde nach Schwere (konsolidiert)

### Kritisch

| # | Thema | Quellen | Kurz |
|---|---|---|---|
| K1 | Refresh-Revocation wirkungslos | SEC-02, BE-01, DB-01, DB-10, BE-32, TST-12 | Bei einem Redis-Treffer wird rotiert, ohne `is_revoked`, `expires_at`, `is_active` oder den Lock in Postgres zu prüfen. Dadurch funktionieren Reuse-Detection, Logout-all, Admin-Revoke und „Sessions bei Passwort-Reset widerrufen“ nicht. Der vorhandene Reuse-Test gibt falsche Sicherheit. |
| K2 | Default-Secrets in Produktion | SEC-01 | Die Defaults im Code sind öffentlich. Der Swarm-Stack übergibt `JWT_SECRET_KEY` und `ADMIN_*` gar nicht, und `/api/admin` ist über das Strip-Prefix erreichbar. Per PoC ließen sich Admin-Cookie und JWT fälschen. *Der Compose-Teil liegt außerhalb des Scopes, ist aber kritisch.* |
| K3 | DB-Passwort im Log | OBS-01, SEC-05, BE-06, DB-09 | Betroffen sind `database.py:88` (`url=settings.DATABASE_URL`) und `alembic/env.py:49` (`print`). Wurden diese Logs nach Loki oder OTLP verschickt, muss das Passwort rotiert werden. |
| K4 | Alembic sieht keine Modelle | DB-02 | Der Autogenerate erzeugt Drops für alle 5 Tabellen, `alembic check` kann nicht bestehen. Ein bloßer Import der Modelle in `env.py` crasht wegen Seiteneffekten beim Import (siehe H6). |

### Hoch

| # | Thema | Quellen |
|---|---|---|
| H1 | Rotation nicht atomar (`GET`, dann `DELETE`): Zwei gleichzeitige Refreshes forken die Family unbemerkt. Das ist auch die Ursache von Frontend-Task 5. | SEC-03, BE-02, DB-03 |
| H2 | Pre-Account-Takeover: Ein Angreifer registriert die Adresse des Opfers, behält seine Session und überlebt den Passwort-Reset des Opfers (Known Issue 5 plus K1). | SEC-04 |
| H3 | DoS am Login: Argon2 blockiert den Event-Loop (rund 41 ms pro Verify, Stalls bis 121 ms). Dazu kommen Advisories in Starlette und multipart über den urlencoded Login. Es gibt weder Body-Limit noch Timeout noch Rate-Limit. | SEC-06, BE-03 |
| H4 | Lockout-Zähler mit Lost Update: Parallele Fehlversuche umgehen die Sperre, und der Zähler verfällt nie. | SEC-07, BE-09, DB-04 |
| H5 | Admin-Panel: Aktionen sind No-ops. Ein Lock ohne `locked_until` hebt sich selbst auf. Lock und Deactivate beenden keine Sessions. `must_change` wird nie durchgesetzt. sqladmin 0.25 hat Advisories, unter anderem die Sortierung nach `password_hash`. Die Session-Cookies sind nicht gehärtet, die GET-Aktionen sind CSRF-anfällig, und es gibt einen Open Redirect über den Referer. | SEC-09, SEC-10, BE-07, DB-05, DB-27 |
| H6 | Bootstrap: `taskiq.py` initialisiert Settings beim Import neu, sodass zwei Settings-Objekte pro Prozess existieren und ggf. `.env.local` gelesen wird. Ohne `app.py` lässt sich die App nicht importieren. Das blockiert `fastapi run`, eine App-Factory und Settings als Dependency. | BE-04, BE-24, OBS-18 |
| H7 | Keine Unit of Work: 11 Crud-Funktionen committen selbst. Registrierung, Reset und Profil-Update können halb geschrieben enden. | DB-06, BE-10 |
| H8 | Header-Längen: Ein User-Agent über 512 Zeichen oder ein `X-Device-*` über 255 Zeichen führt zu einer 500 *nach* einem Teil-Commit. Dann fehlen Audit-Zeilen, und mit einem langen UA ist kein Login möglich. | DB-07, SEC-17 |
| H9 | Personenbezogene Daten in Logs: E-Mails, Usernames (sogar im Kontext jeder Log-Zeile), Query-Strings, Referer, `str(e)` aus SQL mit gebundenen Parametern, ein 8-Zeichen-Präfix des Refresh-Tokens. Das verstößt gegen AGENTS.md und DSGVO Art. 5. | OBS-02…05, OBS-22/23, SEC-12 |
| H10 | 500er sind unsichtbar: kein Metrik-Eintrag, kein Access-Log, keine `X-Request-ID`, keine CORS-Header (der Browser meldet dann einen CORS-Fehler). Access-Log und Exception-Log haben keine `request_id`. | OBS-06/07, BE-12, BE-26 |
| H11 | Metrik-Kardinalität unbegrenzt: `request.route` existiert nicht, deshalb erzeugt jeder Scanner-Pfad neue Zeitreihen (ein DoS auf Prometheus). | OBS-08, SEC-14 |
| H12 | taskiq: Retries tun nichts (keine Retry-Middleware), der Broker liefert at-most-once, und Ergebnisse bleiben ohne TTL in Redis. Fehlgeschlagene Mails gehen verloren. | BE-05 |
| H13 | Test-Harness: Der Redis-Container wird nicht benutzt (die Tests laufen gegen lokales Redis, ohne Flush). Die Tests migrieren deine lokale Entwicklungsdatenbank mit. Das Schema entsteht doppelt (Alembic und `create_all`). Treiber und Postgres-Version weichen von Produktion ab, und Event-Loops werden gemischt. | TST-04…07, DB-08, Known Issues 1/2/11 |
| H14 | Coverage geschönt: Die Regex `"pass"` in `exclude_lines` trifft jede Zeile mit „password“, `concurrency=thread` passt nicht zu async-Code, und die Schwellen widersprechen sich. | TST-01…03 |
| H15 | Abhängigkeiten mit Advisories: starlette 0.52.1, python-multipart 0.0.22, pyjwt 2.11.0, sqladmin 0.25.0, aiosmtplib 5.1.0 (STARTTLS-Injection), cryptography, certifi, pydantic-settings (`secrets_dir`). | researcher, SEC §4 |
| H16 | JWT: `exp`, `sub` und `jti` sind nicht Pflicht, ein Token ohne `exp` läuft nie ab. Ein fehlendes `sub` wird zu `"None"` bzw. führt zu 500. Es fehlen `iss`, `aud` und `typ`. Der Username (personenbezogen) steht in den Claims. | SEC-16, BE-21, TST-13 |

### Mittel (Auswahl, vollständig in den Einzelberichten)

| # | Thema | Quellen |
|---|---|---|
| M1 | Account-Enumeration: Timing (kein Dummy-Hash), `X-Process-Time`, unterschiedliche 409-Meldungen, Lock-Meldung mit Sekundenangabe, Forgot-Password liefert 500 nur bei existierenden Adressen | SEC-08, BE-15, SEC-22 |
| M2 | Keine absolute Session-Lebensdauer: Die 30 Tage gleiten unbegrenzt weiter. | SEC-11 |
| M3 | E-Mail-Tokens sind nicht atomar single-use. Eine Passwortänderung invalidiert offene Reset-Tokens nicht. Es gibt keine Benachrichtigungs-Mails. Die Passwortänderung ist ohne Versuchszähler brute-forcebar. | SEC-19, BE-08, BE-18, DB-15 |
| M4 | E-Mail-Change unfertig (Known Issue 6): keine Re-Auth, Mail an beliebige Adressen (Spam-Relay), Template wiederverwendet, „already in use“ verrät Existenz | SEC-15, BE-18 |
| M5 | Login-CSRF und fehlender Origin-Check auf `POST /auth/*`. Cookie ohne `Secure`, Pfad hinter `/api` falsch (Known Issue 8, Frontend-Task 4). | SEC-13, BE-31 |
| M6 | Zwei Fehlerformate auf dem Draht. Envelope-Metadaten sind falsch (`request_id` passt nicht zum Header, naive Zeitstempel). 401 ohne `WWW-Authenticate`. | BE-11, BE-13, BE-14 |
| M7 | Keine Route hat ein `response_model`, die OpenAPI-Schemas sind leer, und alles läuft über `JSONResponse` am Pydantic-Serializer vorbei. | BE-20 |
| M8 | Fake-Daten in `UserResponse`: `roles=["test"]`, `posts:*` und fest verdrahtete Preferences. 2FA-Stubs stehen im Contract. | BE-17, BE-27, SEC (D3) |
| M9 | E-Mail-Validierung: `$` akzeptiert einen Zeilenumbruch am Ende (Duplikat-Account per PoC). Die Reset-Mail geht an den Request-String statt an `user.email`. Mail ignoriert `Settings` (liest `os.getenv`). Jinja-Autoescape ist aus. | SEC-18, SEC-20, BE-19, BE-28 |
| M10 | Eindeutigkeit von Username und E-Mail nicht case-insensitiv in der DB; Check-then-Insert-Race führt zu 500 | DB-13, DB-14 |
| M11 | Schema-Hygiene: keine Naming-Convention, keine Server-Defaults, `updated_at` von Hand gepflegt, fehlende CHECKs und Indizes, native Enums für Wertemengen, die sich weiterentwickeln, `downgrade` lässt die Enum-Typen stehen | DB-11, DB-12, DB-16…19 |
| M12 | Engine und Session: Pool und Timeouts, `hide_parameters`, Lifecycle von `get_db` (Commit nach der Response), unsichere Lazy-Loads | DB-20, DB-21, DB-25 |
| M13 | Keine Cleanup- und Aufbewahrungs-Jobs für Sessions, Tokens und Audit-Log; kein Löschpfad (DSGVO) | DB-23 |
| M14 | Health: keine Trennung live/ready/startup; der Healthcheck hängt an `METRICS_ENABLED` | OBS-16, OBS-17, Known Issue 12 |
| M15 | Logging-Setup: `uvicorn.error` und SQLAlchemy verlieren ihre Handler; OTLP-Export über einen deprecated `LoggingHandler` mit Dict-Repr als Body, jeder Eintrag doppelt | OBS-14, OBS-15, OBS-19…21 |
| M16 | `/metrics`: der `KEYS`-Scan ist defekt, der Skip-Pfad greift nie (Known Issues 3 und 4), unbrauchbare Size-Buckets, keine Process-, GC- und Auth-Metriken | OBS-09…12, OBS-24 |
| M17 | `PATCH /user/me` kann `display_name` nicht leeren; Logout ist nicht idempotent; `GET /` liefert 500, wenn Caching aus ist | BE-16, BE-22, BE-23 |
| M18 | Trusted Proxies: Audit-IPs zeigen auf Traefik, das Access-Log vertraut einem fälschbaren XFF. **Damit wird jedes Rate-Limit pro IP faktisch global (blockiert D4).** | SEC §3, OBS-05 |
| M19 | Testlücken: keine Zeitkontrolle (Ablauf von Tokens und Locks ungetestet), `captured_emails` speichert nur das letzte Token, kein Test für 500-Handler und Envelope, keine Race-Tests | TST-10, TST-11, TST-18 |

### Niedrig
`mypy src` meldet 6 Fehler in `admin.py`, `ruff format` schlägt fehl (Known Issue 13), das `HTTP_422`-Alias ist deprecated, die Cache-Regel in AGENTS.md (statischer Key) würde Daten zwischen Usern leaken, sobald sie auf authentifizierte GETs angewendet wird (SEC-21), `X-Request-ID` wird nicht validiert, und es gibt diverse Doku-Drift. Die Details stehen in den Einzelberichten.

### Known Issues 1–14 im Abgleich
- **Bestätigt:** 1–9, 11–13. Die Issues 1, 2 und 11 sind schlimmer als beschrieben (H13).
- **Teilweise falsch:** 10. `CACHE_KEY_PREFIX` steht nur im Template. Weitere Settings sind ungenutzt: `TASKIQ_RETRY_*`, `TASKIQ_METRICS_PORT`, `ROUTE_CACHE_TIME_CONDITION`, `DEBUG`, `MAIL_*`.
- **Nicht reproduzierbar:** 14. Die venv läuft mit Python 3.14.4.
- **README-Annahme vermutlich nicht umsetzbar:** „Readiness flippt bei SIGTERM auf 503“. uvicorn schließt den Listener vor dem Lifespan-Shutdown.

---

## 3. Modernisierung: verifizierter Stand 2026-11

| Paket | gelockt | aktuell | Hinweis |
|---|---|---|---|
| fastapi | 0.136.1 | 0.163.1 | Natives OTel (`FastAPI(telemetry=)`, 0.142), `root_path` deprecated (0.146), **0.151: explizites `null` ≠ weggelassen**, Formular-Änderungen (0.147–0.162), Router-Baum (0.137), `Depends(max_concurrency=)` (0.163). RFC 9457 ist nicht im Core. |
| starlette | 0.52.1 | 1.10.0 | 1.x entfernt `on_event` und Co., `max_body_size` (1.6), TestClient streamt (1.9) |
| sqlalchemy | 2.0.49 | 2.1.8 | psycopg 3 ist jetzt Default-Treiber, Autoflush unbedingt, neue `Select`-Typisierung |
| pydantic / -settings | 2.13.3 / 2.14.0 | 2.14.3 / 2.16.3 | |
| redis-py | 7.4.0 | 8.1.0 | Major-Version |
| pyjwt | 2.11.0 | 2.15.1 | Advisories |
| sqladmin | 0.25.0 | 0.32.0 | Advisories; Kompatibilität mit SQLAlchemy 2.1 ungeprüft |
| uvicorn | 0.40.0 | 0.54.0 | |
| mypy / ruff | 1.19.1 / 0.15.0 | 2.4.1 / 0.17.5 | |
| taskiq | 0.12.2 | 0.13.0 | |
| opentelemetry | 1.39.1 | 1.45.2 | `LoggingHandler` deprecated |

Python 3.14 bringt `uuid.uuid7()` in der Standardbibliothek und PEP 649 (für Pydantic unkritisch). Asyncio-Policies sind deprecated und fallen in 3.16 weg; die Tests nutzen noch `WindowsSelectorEventLoopPolicy`. Python 3.15 ist erschienen, 3.14 bleibt trotzdem das Ziel.

---

## 4. Umsetzungsplan

Leitlinien:
- Jede Phase ist für sich lauffähig. Für jeden Bugfix wird zuerst ein roter Regressionstest geschrieben.
- Breaking Changes am API-Vertrag sind in **einem** Release (Phase 3) gebündelt, damit das Frontend nur einmal migrieren muss.
- Commits macht nur der User.

### Phase 0: Sofortmaßnahmen ohne Vertrags- und Schemaänderung
*Agents: backend-engineer (Code), backend-tester (Race- und Regressionstests). Aufwand: M.* **Status: umgesetzt (uncommitted); nach Phase 0b 223 Tests grün, ruff und mypy sauber. Known Issues 1, 3, 5, 13 behoben, 14 nicht reproduzierbar, 10 und 11 teilweise.**
- [x] 1. **K3:** DB-URL maskieren (`database.py`, `alembic/env.py`), `hide_parameters=True`, Präfix-Log des Refresh-Tokens entfernen. *Empfehlung: DB-Passwort rotieren, falls Logs zentral gesammelt wurden.*
- [x] 2. **K1 + H1:** Postgres wird das einzige, atomare Gate für den Refresh (`UPDATE … WHERE NOT is_revoked AND expires_at>now() RETURNING`), und zwar mit Prüfung von `is_active` und Lock. Der Redis-Cache für Refresh-Tokens entfällt. Damit sind auch SEC-12 (Teil 1) und DB-10 erledigt. Der Reuse-Test wird korrigiert.
- [x] 3. **K2 (App-Teil):** Pflicht-Secrets als `SecretStr` und ein Startup-Guard außerhalb von `dev`/`unittest`. Dazu `ADMIN_ENABLED` mit Default `false` außerhalb von `dev`.
- [x] 4. **H15:** Sicherheits-Upgrades innerhalb der aktuellen FastAPI-Grenzen: starlette ≥1.3.1, python-multipart ≥0.0.33, pyjwt 2.15.1, sqladmin 0.32, aiosmtplib, cryptography, certifi, pydantic-settings. *(Ist-Stand: starlette 1.10, FastAPI weiterhin 0.136.1; Constraints in `[tool.uv] constraint-dependencies`.)*
- [x] 5. **H3:** Argon2 über `anyio.to_thread` mit `CapacityLimiter`, Parameter in `Settings`, `check_needs_rehash`.
- [x] 6. **H4, M3 (Teil):** atomarer Lockout-Zähler; E-Mail-Tokens atomar single-use.
- [x] 7. **H2 und Known Issue 5:** beim Reset, bei der Passwortänderung, beim Lock und beim Deactivate alle Sessions widerrufen; `POST /auth/logout-all`. *(Reset und Passwortänderung widerrufen Sessions, Passwortänderung außer der des Aufrufers; `POST /auth/logout-all` neu; Refresh verweigert und Familie widerrufen bei inaktiven oder gesperrten Usern.)*
- [x] 8. **H5 (Teil):** Admin-Aktionen lesen `?pks=`, ein Admin-Lock bleibt bestehen, `must_change` wird durchgesetzt. *(Teil: `?pks=`, fester Redirect, konstante Zeitvergleiche, Admin-Lock unbefristet erledigt; `must_change`-Durchsetzung bewusst nach Phase 3 verschoben, weil sie einen neuen Fehlercode im Vertrag braucht.)*
- [x] 9. **H8, M9 (Teil):** Header-Werte kürzen; `EmailStr` statt Regex; Reset-Mail an `user.email`. *(Erledigt: Header-Kürzung auf 512/255, `EmailStr` mit Ablehnung von Whitespace, Reset-Mail an `user.email` (`service.py:572`). Offen: `UpdateProfileRequest.email` nutzt noch die alte Regex.)*
- [x] 10. **H16 (nicht brechender Teil):** `exp`, `sub` und `jti` werden Pflicht, der Algorithmus wird im Code fest auf HS256 gesetzt.
- [x] 11. **H11:** Routen-Template als Metrik-Label, `unmatched` als Fallback.

### Phase 0b: Nachzug aus dem Re-Audit (vor dem Commit von Phase 0)
*Agents: backend-tester (rote Tests zuerst), backend-engineer (Fixes), docs-writer. Kein Vertragsbruch, keine Migration.* **Status: umgesetzt (uncommitted), 223 Tests grün, ruff und mypy sauber.** Die NEW-IDs stammen aus dem Re-Audit durch security-auditor.
- [x] NEW-1. **Race zwischen Refresh und Revocation:** Row-Lock auf `users` (`SELECT … FOR NO KEY UPDATE`, `crud.lock_user`) vor dem Refresh-Gate und vor `revoke_user_sessions` und `revoke_family`. Globale Lock-Reihenfolge: `email_tokens` → `users` → `sessions`.
- [x] NEW-2. **Login-Burst:** Admission-Limiter pro Prozess (`PASSWORD_HASH_CONCURRENCY`), Lock-Re-Check nach der Zulassung und ein bedingter atomarer Erfolgs-UPDATE (kein Login mehr, sobald der Lock committet ist). Bei N Worker-Prozessen gilt die Schranke N × Wert. Rate-Limiting (D4) bleibt die Hauptkontrolle und offen.
- [x] NEW-3. **Timed Lock sperrt nur den Passwort-Login** (Entscheidung User, 2026-11-16): Bestehende Sessions, Access-Tokens und Refresh bleiben gültig, damit niemand per Fehlversuchen fremde Nutzer abmelden kann. Nur der Admin-Lock (`locked_until IS NULL`, `User.is_admin_locked()`) lehnt Access-Tokens sofort ab und beendet Sessions beim Refresh; die Admin-Aktion „Lock selected“ widerruft alle Sessions.
- [x] NEW-4. **`IntegrityError` → 409, keine PII in DB-Fehler-Logs:** Parallele Doppel-Registrierung liefert 409 mit der normalen Meldung statt 500 (`service.py`, Mapping der Constraints `users_username_key` und `users_email_key`). `PATCH /user/me` schreibt `users.email` noch nicht (das `CHANGE_EMAIL`-Token hat keinen Consumer, Known Issue 6), dort gibt es keinen Constraint-Race. Fehler-Logs tragen `error_type`, bei DB-Fehlern zusätzlich `db_error_type`, `sqlstate` und `constraint`, aber keinen Exception-Text; Tracebacks von DB-Fehlern werden nicht geloggt (`log_unexpected_error`, `src/common/db_errors.py`).
- [x] NEW-5. **`revoke_family` committet immer**, auch ohne aktive Zeilen. Dadurch gibt es keinen falschen Reuse-Alarm im Pfad „Account deaktiviert, einzige Session“.
- [x] NEW-7. **Robustheit:** Metrik-Label `method` auf GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS und `other` begrenzt; Untergrenzen für `ARGON2_TIME_COST` ≥ 2, `ARGON2_MEMORY_KIB` ≥ 19456, `ARGON2_PARALLELISM` ≥ 1 (OWASP-Minimum) und `PASSWORD_HASH_CONCURRENCY` ≥ 1, in jeder Umgebung validiert.
- [x] anyio ≥ 4.14.2 als Untergrenze in `[tool.uv] constraint-dependencies` (gelockt: 4.14.2), weil anyio auf dem Login-Pfad liegt.

**Offener Backlog nach Phase 0b** (Zielphase in Klammern):
- `revoke_session` (Logout) überschreibt `revoke_reason` `rotated`. Ein späteres Replay des Tokens wird dann nicht als Reuse erkannt. (Phase 3)
- Der Login hält seine Pool-Verbindung, während er auf Admission- und Argon2-Slots wartet. Ein Burst kann den DB-Pool erschöpfen. (Phase 2, Unit of Work, und Phase 4, Rate-Limiting)
- Das 409-Mapping hängt an den Default-Constraint-Namen `users_*_key` (`users_username_key`, `users_email_key`) und muss der Naming-Convention aus Phase 2 folgen. (Phase 2)
- NEW-8: Stärke der Admin-Secrets, zusammen mit D2. (Phase 4)
- NEW-9: Unicode im Local-Part der E-Mail-Adresse. (Phase 3)
- `deployment/` ist git-ignored, der Compose-Secret-Fix ist daher nicht versioniert. (Deploy-Runde, Entscheidung User)
- Verbleibende `uv audit`-Advisories: urllib3, dnspython, aiohttp, mako, opentelemetry-api. (Phase 1, Dependency-Pass)

### Phase 1: Fundament und Tooling
*Agents: backend-engineer, backend-tester, db-engineer. Aufwand: L. Ändert `pyproject.toml` und `conftest.py`.*
1. `pyproject.toml`: `requires-python >=3.14`, Test- und Lint-Tools raus aus den Runtime-Dependencies (PEP 735 Groups), `uv_build`, eine einzige Versionsquelle, strikte Ruff-Konfiguration, `mypy --strict` schrittweise, `ty` als Hinweis, Coverage-Konfiguration korrigiert (H14).
2. **H6:** App-Factory `create_app(settings)`, `get_settings()` gecacht als Dependency, keine Seiteneffekte beim Import (Engine, Redis, Mail, taskiq lazy im Lifespan), `[tool.fastapi] entrypoint`, `fastapi run`/`fastapi dev`. `app.py` wird ein dünner Wrapper.
3. **K4:** Model-Registry, `env.py` ohne `.env.local`-Override und ohne Print, `alembic check` als Test.
4. **H13, M19:** Test-Harness neu aufsetzen:
   - `httpx.AsyncClient` mit `ASGITransport` und Lifespan einmal pro Session,
   - Testcontainers lazy mit Postgres 18 und demselben Treiber wie in Produktion, Redis über die Settings verdrahtet,
   - `TRUNCATE` und `FLUSHDB` *vor* jedem Test,
   - time-machine,
   - ein `captured_emails`, das alle Mails mitschreibt,
   - Layout `unit/integration/db/contract`.
5. Major-Upgrades: FastAPI 0.163, Starlette 1.10, SQLAlchemy 2.1, redis-py 8, mypy 2, ruff 0.17, taskiq 0.13, OTel 1.45. Danach folgen die Verhaltens-Checks (`null`-Semantik, Formular-Login, Router-Baum).
6. **H12:** taskiq bekommt eine Retry-Middleware, Ergebnis-TTL und Acks. E-Mail-Tokens erzeugt der Worker selbst; die Task bekommt nur `user_id` und `purpose`.

### Phase 2: Datenschicht
*Agent: db-engineer. Aufwand: L.*
1. Mixins (UUID-PK, `created_at`/`updated_at` mit Server-Default, optional `version`, optional Soft-Delete) und eine Naming-Convention.
2. **H7:** Unit of Work. Der Service committet einmal pro Use Case, `get_db` committet nicht. Dazu ein `transaction()`-Helfer. Die Regel in AGENTS.md wird entsprechend angepasst.
3. **D10:** UUIDv7-Primärschlüssel.
4. **M10, M11:** `citext` bzw. Unique auf `lower()`, CHECK-Constraints, Indizes passend zu den Queries, Text+CHECK statt nativer Enums, `IntegrityError` wird auf 409 abgebildet.
5. **M2:** absolute Session-Lebensdauer (`family_created_at`).
6. **Neue Migrations-Baseline**, die `37a0d49f8bbc` ersetzt. Voraussetzung: nirgends liegen echte Daten auf diesem Stand. Den Round-Trip Upgrade → Downgrade → Upgrade sichert ein Test ab.
7. **M13:** taskiq-Cron-Jobs für Cleanup und Aufbewahrung (Sessions, Tokens, Audit-Log).
8. Festlegung auf **einen** Postgres-Treiber (Entscheidung N1).

### Phase 3: API-Vertrag v1 (ein gebündelter Breaking Release)
*Agents: backend-engineer; docs-writer für die Frontend-Tasks in `../frontend/documentation/backend-contract.md`. Aufwand: L.*
1. Alle Router unter `/api/v1`. Health und Metrics bleiben ohne Version. `root_path` per `fastapi run --root-path`.
2. **D1:** Fehler als RFC 9457 (`application/problem+json` mit `code` und `request_id`), Erfolg als reines `response_model`. Ein Exception-Hierarchie-Handler, auch für 404, 405 und 500, inklusive CORS-, Request-ID- und Security-Headern (H10, M6).
3. **M7:** `response_model`, `summary` und `responses` auf jeder Route; OpenAPI vollständig; kein `JSONResponse` mehr.
4. **M8:** Fake-Rollen, Preferences und 2FA-Stubs entfernen (Rollen erst mit D3).
5. **M5:** Refresh-Cookie mit `Secure` und `__Host-` (oder `__Secure-`, falls ein Pfad nötig ist), `max_age`, explizites SameSite, Origin- und `Sec-Fetch-Site`-Check auf `POST /auth/*`.
6. **M1:** Enumeration-Fixes: Dummy-Hash, einheitliche 401, Register antwortet `202` (**Breaking**), kein `X-Process-Time` in Produktion.
7. **H16 (Rest):** `iss`, `aud`, `typ=at+jwt`; Username raus aus den Claims.
8. **M4:** E-Mail-Change komplett: Re-Auth, `POST /auth/email/confirm`, Benachrichtigung an die alte Adresse, Audit. **M3:** Benachrichtigungs-Mails nach Passwortänderung und -reset.
9. Frontend-Tasks 1–5 aus `backend-contract.md`: Einladungen, E-Mail-Links über `FRONTEND_URL` (Known Issue 7), eigene Token-Fehlercodes, Cookie-Pfad, Reuse-Intervall.
10. **M17:** `PATCH`-Semantik (`exclude_unset`, `extra="forbid"`), idempotenter Logout.

### Phase 4: Security-Features
*Agents: backend-engineer, security-auditor (Re-Audit am Ende). Aufwand: M–L.*
1. **M18:** Trusted Proxies (`forwarded_allow_ips`), eine einzige Quelle für die Client-IP.
2. **D4:** Rate-Limits in der App (`limits` mit Redis, per Account+IP, Forgot-Password, Mail-Endpoints, Refresh-Family) mit 429 und `Retry-After`; Backoff statt hartem Lock.
3. **D2:** Admin-Panel härten oder entfernen.
4. **D3:** generisches RBAC (`roles`, `permissions`, `require_permissions`, deny-by-default).
5. Security-Header (HSTS, `no-store` auf Auth-Responses, CSP `frame-ancestors`), Body-Limit, Request-Timeout.
6. Jinja-Autoescape für Mails, Cache-Regel in AGENTS.md korrigieren (kein `@cache` auf Routen mit User-Bezug).

### Phase 5: Observability
*Agent: observability-engineer. Aufwand: M.*
1. Eine einzige pure-ASGI-Middleware für Request-ID (validiert), Contextvars, Access-Log, Metriken und das Erfassen von 500ern. Security-Header ebenfalls als ASGI.
2. Logging einmal zentral einrichten, nur JSON auf stdout, kein OTLP-Log-Export im Prozess. Felder: `request_id`, `trace_id`, `span_id`, `user_id`. Ein Test prüft die Logs auf personenbezogene Daten.
3. Metriken nach den OTel-Semconv-Namen (`http_server_request_duration_seconds`), dazu Process- und GC-Collectors, Auth-Events, DB-Pool, Cache sowie taskiq-Metriken.
4. `/health/live`, `/health/ready` und `/health/startup`, ohne Envelope und ohne Metrik-Flag.
5. Tracing über das native `FastAPI(telemetry=)` plus SQLAlchemy-, Redis- und taskiq-Propagation.

### Phase 6: Erweiterbarkeit und Doku
*Agents: backend-engineer, docs-writer. Aufwand: M.*
1. Getestetes Template-Modul `src/api/_template/`, eine Domain-Exception-Hierarchie, ein Keyset-Pagination-Helfer.
2. **D9:** opt-in `Idempotency-Key` (in Postgres in derselben Transaktion, Redis-Lock für laufende Requests, **422** bei abweichendem Payload, 409 bei einem parallel laufenden Request).
3. README, AGENTS.md und `documentation/*` an den Code angleichen; die getroffenen Entscheidungen als ADRs (MADR) festhalten; CHANGELOG; SECURITY.md (D8).

**Separat und außerhalb des Scopes (Empfehlung: deploy-engineer in einer eigenen Runde):** Secrets im Swarm-Stack als Docker Secrets, Admin und Metrics nicht öffentlich routen, Postgres 18, Healthchecks auf `/health/*`, Python-3.14-Image.

In Phase 0 umgesetzt (Ausnahme K2): `api`, `taskiq-worker` und `taskiq-scheduler` bekommen `JWT_SECRET_KEY: ${JWT_SECRET_KEY:?…}`, sodass das Rendern fehlschlägt, wenn der Wert fehlt oder leer ist. Zusätzlich bekommt `api` `ADMIN_ENABLED: "false"`.

Offene Punkte für die Deploy-Runde, festgestellt von deploy-engineer am 2026-11-16:
- **Secrets:**
  - Swarm-Secrets über pydantic-settings `secrets_dir`.
  - `DB_PASSWORD:-postgres` und `REDIS_PASSWORD:-redispw` haben öffentliche Fallbacks.
  - `docker stack deploy` liest keine `.env`, das muss im README stehen.
- **Routing:**
  - `/api/admin`, `/docs`, `/openapi.json` und `/metrics` sind über Traefik öffentlich erreichbar.
  - Der Router läuft auf dem Plain-HTTP-Entrypoint `web`.
- **Traefik:** Version 2.11 mit rohem Docker-Socket.
- **Netz:** Das `internal`-Overlay ist weder `internal: true` noch verschlüsselt.
- **Images und Ports:**
  - Die Images nutzen `:latest`.
  - Die Worker-Metriken sind auf Port 9000 veröffentlicht.
  - Das taskiq-Dashboard ist öffentlich.
- **Admin:** Wird `ADMIN_ENABLED=true` gesetzt, braucht es zusätzlich `ADMIN_*` mit `:?`.
- **Migrate-Service:** Liest `alembic/env.py` später `Settings`, braucht auch `migrate` das `JWT_SECRET_KEY`.

---

## 5. Entscheidungen

### Offene Entscheidungen aus dem README

| # | Thema | Empfehlung der Agents |
|---|---|---|
| D1 | Fehlerformat | **RFC 9457** für Fehler (Erweiterungsfelder `code` und `request_id`), Erfolg als reines `response_model`; zusammen mit `/api/v1` ausliefern |
| D2 | Admin-Panel | **Opt-in behalten**: `ADMIN_ENABLED=false` außer in `dev`, nie öffentlich routen, sqladmin 0.32, eigene Admin-Accounts über RBAC statt eines geteilten Logins, gehärtete Session, Audit. Ist das zu aufwendig, entfernen. |
| D3 | Autorisierung | **Kleines generisches RBAC jetzt** (4 Tabellen, `require_permissions`, deny-by-default); die Platzhalter sofort entfernen; keine Rechte im JWT ohne `perm_ver` |
| D4 | Rate-Limits | **Beides**: Traefik grob pro IP, die App fein (Account+IP, E-Mail, Family) mit `limits` und Redis; Trusted Proxies vorher |
| D6 | Type-Checker | **mypy strict** als Gate, `ty` (Beta) als Hinweis; kein Pyright als drittes Gate |
| D8 | Compliance | **CRA/NIS2-Deliverables als Pflicht behandeln**: SECURITY.md, SBOM, `uv audit`, sichere Defaults, Log-Minimierung |
| D9 | Idempotency | **Phase 6, opt-in**: Datensatz in Postgres in derselben Transaktion; 422 bei abweichendem Payload (IETF-Draft) statt der 409 aus der Roadmap |
| D10 | Primärschlüssel | **UUIDv7** über `uuid.uuid7()`; DB-Default `uuidv7()` erst, wenn Postgres 18 überall gesetzt ist; v4 als Opt-out pro Tabelle |
| D12 | Hashing | **argon2-cffi behalten**; Parameter in `Settings`, `check_needs_rehash`, Hashing außerhalb des Event-Loops |

Ohne Bewertung, weil Deployment und CI nicht im Scope sind: D5, D7, D11.

### Neue Entscheidungen aus dem Review

| # | Frage | Default der Agents |
|---|---|---|
| N1 | Postgres-Treiber: asyncpg oder psycopg 3 | Einen festlegen. psycopg 3 ist Default in SQLAlchemy 2.1, funktioniert sync und async und wird schon in den Tests genutzt; asyncpg ist etabliert. |
| N2 | Liegen irgendwo echte Daten auf Revision `37a0d49f8bbc`? | Nein, also neue Baseline |
| N3 | Postgres 18 überall (Tests, Compose, Swarm)? | Ja |
| N4 | Usernames case-insensitiv eindeutig? | Ja |
| N5 | Login für unverifizierte Accounts sperren? | Nein; Sessions vor der Verifizierung werden aber widerrufen |
| N6 | Grace-Window beim Refresh (zweiter Tab)? | Kein Grace-Window bzw. nur das enge Reuse-Intervall aus Frontend-Task 5 |
| N7 | Laufen Frontend und API same-site (dann `SameSite=Strict` möglich)? | offen |
| N8 | Aufbewahrung von Logs und Audit-Log; IP voll oder gekürzt speichern? | Audit 365 Tage, konfigurierbar; IP im Access-Log gekürzt |
| N9 | HTTP-Metriken über Prometheus oder OTel/Alloy? | Prometheus |
| N10 | Wird das Produkt im EU-Markt in Verkehr gebracht (CRA)? | Ja, als Annahme |
| N11 | Den kritischen Compose-Fix (K2) trotz Scope jetzt mitnehmen? | Ja, als eng begrenzte Ausnahme |
