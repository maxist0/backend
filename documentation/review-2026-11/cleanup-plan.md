# Cleanup 2026-11: Stand und Übergabe (S0–S3)

Dieses Dokument enthält alles, was eine neue Session braucht, um das Backend-Cleanup fortzusetzen. Eingeschoben wurde es am 16.11.2026 vor Schritt 1.5 des [Review-Plans](README.md#4-umsetzungsplan). Gemeint ist eine Claude-Code-Session, die im Workspace-Root `Prooduction/` startet und die Projekt-Agents nutzt.

**Ziel des Cleanups:** Das Backend ist ein **Modul-Boilerplate**. User-Management, Skalierung und Production-Features stecken im Core. Eine neue App fügt nur Module hinzu und ändert den Core nicht. Einfachheit hat Vorrang: Mechanik wird nur gebaut, wenn es den Fall, für den sie gedacht ist, auch wirklich gibt.

## 1. Verbindliche Quellen (vor jeder Arbeit lesen)

| Was | Wo |
|---|---|
| Konventionen (einzige Quelle): Modul-Layout, Core vs. Module, Fehler und Responses, Hausstil für Kommentare, Testaufteilung, Boundaries | [`backend/AGENTS.md`](../../AGENTS.md) |
| Entscheidungen mit Begründung | [`documentation/adr/`](../adr/README.md), ADR 0001–0008 |
| API-Vertrag (Stand S1.3) | [`frontend/documentation/backend-contract.md`](../../../frontend/documentation/backend-contract.md) |
| Known issues und Roadmap | [`backend/README.md`](../../README.md#known-issues) |
| Gemeinsamer Standard aller Agents | `.claude/agent-standards.md` (Workspace-Root) |
| Erkenntnisse der Agents aus S1 | `backend/.claude/agent-memory/backend-engineer/*.md` (git-ignored, lokal) |

## 2. Arbeitsweise (vom Owner festgelegt)

- **Erst fragen, dann bauen.** Strukturfragen, Vertragsänderungen und alles Sicherheitsrelevante werden dem Owner vorher als Fragen mit Empfehlung vorgelegt. Offene Entscheidungen (D2–D4, D8–D10, D12 im README) werden nie still entschieden.
- **Schritte klein halten.** Nach jedem Unterschritt meldet die Session dem Owner das Ergebnis und schlägt eine Commit-Nachricht vor. **Commits macht der Owner.** Erst danach beginnt der nächste Schritt.
- **Agents arbeiten, die Hauptsession prüft.** Code schreibt backend-engineer, Tests backend-tester (oder backend-engineer mit), Doku docs-writer, Reviews macht security-auditor. Die Hauptsession wiederholt nach jedem Agent-Lauf selbst die Verifikation (siehe §6) und liest die Kernstellen gegen.
- **Aufträge an Agents nie auf Annahmen bauen.** In S1.3 waren drei „wie heute“-Aussagen im Auftrag falsch. Vor jeder Aussage über das heutige Verhalten den Code lesen, und jeden Agent anweisen: „Wenn der Code dem Auftrag in sicherheits- oder vertragsrelevanten Punkten widerspricht, stoppen und Rückfragen stellen.“
- **Security-Review am Ende jedes Schritts, der Auth, Tokens, Mails oder Middleware berührt.** Funde werden vor dem Commit behoben, oder der Owner akzeptiert sie ausdrücklich und sie werden als Known issue dokumentiert.
- **Grenzen:** nie committen oder pushen, nie deployen, Migrationen nur auf Wegwerf- oder Test-DBs anwenden. Für Smoke-Tests ein eigenes Compose-Projekt (`docker compose -p <name> -f compose.dev.yaml ... up -d --wait` mit Nicht-Standard-Ports per `DEV_POSTGRES_PORT`/`DEV_REDIS_PORT`, danach `down -v`). Die Dev-DB und `.env.local` des Owners werden nie angefasst. `deployment/` wird nicht geändert; Punkte dafür kommen in die Liste der Deploy-Runde (§7).

## 3. Stand

| Schritt | Inhalt | Commit |
|---|---|---|
| S0 | Konventionen in `AGENTS.md`, ADR 0001–0007, Agent-Dateien | `38ed4b9` |
| S1.1 | Tests aufgeteilt in `tests/unit` (ohne Docker) und `tests/integration` (Testcontainers) | `e9c2f74` |
| S1.2 | `core/` + `modules/{users,auth}`, `Module`-Vertrag, festes Datei-Vokabular, Shared Broker, ADR 0008 | `03634f9` |
| S1.3 | RFC 9457, Plain-Responses, `/api/v1`, `/users/me`, ausstehender E-Mail-Wechsel, einheitliche Login-Fehler | `f94ed50` |
| S1.4 | Eine ASGI-Middleware, `/health` + `/health/ready`, `/metrics`, `@cached` (JSON), Logging nur über stdout, Config bereinigt | `7b8a506` |
| S1.5 | Admin nur lesend und über Services, Admin-CSRF, Fixes aus dem Security-Review (H1, M1, M2, L3–L5) | `adf8b66` |
| S2.1 | Redis-Stream-Broker mit Acks, verzögerte Retries, Worker-Metriken, Windows-Worker, ADR 0009 | `326cb15` |
| S2.2 | Tokens im Worker neu gesetzt (Task bekommt nur die `token_id`), `TaskDbSession`, bereinigte DB-Fehler in Tasks, absolute Mail-Links, ADR 0010 | `14aae6e` |
| S2.3 | Sperr-Mail, Hinweise an die alte Adresse, nächtlicher Cleanup, Neusperre nach abgelaufener Sperre (Migration `14e82733e84f`) | Commit durch den Owner |
| **S3** | Stil, strikte Checks, Doku | **nächster Schritt** |
| **Frontend** | Umstellung auf den S1.3-Vertrag (plus S2: absolute Links, Refresh-401 löscht das Cookie) | offen |

Stand nach S1.5: 587 Tests grün, Coverage 95,2 %, 271 Unit-Tests ohne Docker. ruff (0.15), mypy und `uv audit` sind sauber.

## 4. Nächster Schritt: Frontend-Umstellung

Seit S1.3 funktioniert das Frontend nicht mehr mit dem Backend. **Empfohlene Reihenfolge: zuerst das Frontend umstellen, dann S2.** S2 ändert den Vertrag nur minimal.

- Agents: frontend-engineer setzt um, frontend-tester sichert ab. Zuerst `frontend/AGENTS.md` und `frontend/README.md` lesen.
- Die Aufgabenliste steht in `frontend/documentation/backend-contract.md`, Abschnitt „Migration from the envelope contract“. Er hat 13 Punkte, darunter:
  - Basis-URL `/api/v1`;
  - Antworten ohne Envelope lesen;
  - ein Parser für Problem-Details (`code`, `detail`, `errors[]`, `request_id`);
  - die Wechsel von PATCH auf PUT;
  - der E-Mail-Wechsel als ausstehender Ablauf (202);
  - Resend mit dem Code `email_already_verified`;
  - einheitliche 401 `invalid_credentials` beim Login;
  - MSW-, Playwright- und Unit-Mocks anpassen.
- Commits im Frontend-Repo macht der Owner.

## 5. S2: Hintergrund-Jobs und Mails zuverlässig machen

Agent: backend-engineer. Am Ende reviewt security-auditor. Vorher die Memory-Notizen des Agents und `src/app/core/tasks.py`, `src/app/worker.py`, `src/app/modules/auth/tasks.py`, `src/app/core/mail.py` und `src/app/modules/auth/service.py` lesen.

**Heutiger Stand (geprüft am 17.11.2026, vor der Umsetzung erneut prüfen):**
- Module-Tasks laufen über `async_shared_broker` (Facade `app.core.task`).
- Der echte Broker wird in `app/worker.py` (`app.worker:broker`/`:scheduler`) und im App-Lifespan gebaut.
- Die Mail-Tasks bekommen Empfänger, Username und **rohes Token** als Argumente. Diese liegen damit in Redis.
- `TASKIQ_RETRY_COUNT`, `TASKIQ_RETRY_DELAY_SECONDS` und `TASKIQ_METRICS_PORT` existieren in den Settings, werden aber nicht gelesen.
- Der Broker ist `ListQueueBroker` mit `socket_timeout=None`; es gibt keine Acks.

**Aufgaben:**
1. **Retries** über die Retry-Middleware von taskiq 0.13 mit den Werten aus `TASKIQ_RETRY_*`. Gezielt wiederholt wird nur bei vorübergehenden Fehlern (SMTP oder Netzwerk), nicht bei Programmierfehlern.
2. **Ergebnis-TTL** im Result-Backend.
3. **Zustellsemantik:** Entscheiden, ob `ListQueueBroker` bleibt oder ein Stream-Broker mit Acks kommt (taskiq-redis). Die Entscheidung mit Quelle begründen und **dem Owner vorlegen**. Wenn sich die Zustellsemantik ändert, ein kurzes ADR schreiben.
4. **Tokens im Worker erzeugen.** Mail-Tasks bekommen nur `user_id` und `purpose`. Der Worker erzeugt das Token, speichert den Hash, rendert und versendet. Das muss idempotent und retry-sicher sein: Ein neuer Versuch invalidiert das vorherige Token desselben Zwecks. In Redis stehen dann weder Token noch E-Mail-Adresse noch Username. Ein Test liest die Redis-Queue aus und beweist das.
   - Gilt für `send_verification_email`, `send_password_reset_email` und `send_email_change_confirmation`. Beim E-Mail-Wechsel muss die neue Adresse im Token stehen, nicht im Task-Argument.
   - Die Lock-Reihenfolge `email_tokens → users → sessions` beibehalten.
5. **Ein Fehler beim Einreihen nach dem Commit führt nicht zu einer 500.** Betrifft vor allem „Passwort vergessen“, das immer 202 antworten muss. Der Fehler wird mit Fehlertyp geloggt.
6. **Mail nicht konfiguriert:** ein klares Log-Event statt stillem Überspringen. Das bisherige `email.verification_sent` ist in diesem Fall irreführend.
7. **Worker-Metriken:** die Prometheus-Middleware von taskiq auf `TASKIQ_METRICS_PORT`. Erfasst werden Tasks ok/fehlgeschlagen, Dauer und, falls verfügbar, die Queue-Länge.
8. **Windows-Dev-Worker:** `run.py dev` startet den Worker unter Windows über einen eigenen Einstieg mit `asyncio.run(..., loop_factory=SelectorEventLoop)`, weil psycopg nicht mit dem ProactorEventLoop läuft. Beim Smoke-Test in S1.4 ist der Dev-Worker abgestürzt und neu gestartet, als Redis gestoppt wurde. Er soll sich stattdessen sauber wieder verbinden.
9. **Neue Mails (vom Owner freigegeben):**
   - „Dein Konto ist bis HH:MM gesperrt“ an den User, wenn die Login-Sperre greift. Die Login-Antwort bleibt einheitlich 401 `invalid_credentials`.
   - Hinweis an die **alte** Adresse, wenn ein E-Mail-Wechsel beantragt und wenn er bestätigt wird („Warst du das nicht? …“).
10. **Cleanup-Task (vom Owner freigegeben):** ein geplanter Task, der abgelaufene und widerrufene Sessions und abgelaufene oder verbrauchte E-Mail-Tokens nach einer Aufbewahrungsfrist löscht. Er ersetzt die in S1.5 entfernte Admin-Aktion. Aufbewahrungsfrist als Setting mit Grenzen, Wert **mit dem Owner abstimmen**.
11. **Kleine Punkte aus S1:**
    - Ein Refresh mit wiederverwendetem Token löscht auch das Cookie (heute nur der Logout).
    - „Passwort vergessen“ verschickt für deaktivierte Accounts keinen Link mehr. Die Antwort bleibt trotzdem 202, damit nichts verraten wird.
    - Mail-Links sind absolute URLs auf `FRONTEND_URL` (Known issue 7: Der Reset-Link ist heute relativ). Die Pfade mit dem Frontend abgleichen: Der E-Mail-Wechsel nutzt heute denselben Pfad wie die Verifizierung.
12. **Tests:** `captured_emails` (patcht heute `.kiq`) wird durch einen Fake- oder InMemory-Broker in `tests/integration` ersetzt. Neue Tests für Retry, Idempotenz, „kein Token und keine Adresse in Redis“, die neuen Mails, den Cleanup-Task und den Enqueue-Fehler.

**Vertragsänderungen fürs Frontend** (in `backend-contract.md` nachtragen): „Passwort vergessen“ antwortet garantiert 202, und die Mail-Links werden absolut. Sonst ändert sich nichts.

## 6. S3: Stil, strikte Checks, Doku

Agents: backend-engineer (Code und Tests), danach docs-writer.

1. **Hausstil in allen Dateien** (`src/`, `tests/`, `run.py`, `alembic/env.py`), nach `AGENTS.md` → „Comments and docstrings“:
   - Pfad-Kopfzeilen, Banner, nacherzählende Kommentare, Review- und Phasen-IDs sowie TODOs ohne Issue entfernen;
   - veraltete Kommentare korrigieren;
   - Google-Docstrings mit einfachen Backticks ergänzen.
2. **ruff 0.17 strict:** `[tool.ruff]` mit `target-version = "py314"` und dem Regelsatz E, W, F, I, B, UP, S, ASYNC, N, D (google), ANN, RUF, PL, PT, SIM, T20, ERA, TRY, FAST, C4, DTZ, EM, LOG, G, PTH, RET, ARG, TC, BLE, FBT, A, ISC, PIE, PERF, FURB, SLF, TID, ICN, INP, PGH.
   - Begründete `per-file-ignores` nur für `tests/**` und `alembic/**`.
   - **Alle Befunde beheben**, kein pauschales `noqa`.
   - ruff kommt in die lint-Gruppe, und alle Befehle laufen danach über `uv run ruff` statt `uvx ruff@0.15.0`. `AGENTS.md`, die Agent-Dateien und dieses Dokument entsprechend anpassen.
3. **mypy strict** für `app`, ohne `ignore_missing_imports`-Ausnahmen, kein `# type: ignore` ohne Code und Grund, dazu `py.typed`. `ty check` wird nur als Hinweis dokumentiert (D6).
4. **Tests aufräumen:**
   - doppelte Helfer zusammenführen (`register`/`login`/`bearer`, `_alice`, `_log_text`);
   - reine Library-Tests löschen und nur Guards auf unsere eigene Konfiguration behalten;
   - `EXPECTED_OPENAPI_PATHS` durch einen OpenAPI-Snapshot-Test ersetzen;
   - Testdateien nach Verhalten benennen statt nach Review-Findings (`test_security_regressions.py`, `test_upgrade_*.py`).
5. **Kleinkram:**
   - `jinja2` als direkte Abhängigkeit eintragen;
   - das Alembic-Template `script.py.mako` auf moderne Typen umstellen;
   - `.env.unittest` löschen (wird von nichts gelesen, Owner-Entscheidung vom 18.11.2026);
   - `core/admin.py`-Helfer bei Bedarf in die Facade aufnehmen.
6. **Doku (docs-writer):**
   - Backend-`README.md` neu: Layout, Quick Start mit `compose.dev.yaml`, Windows-Start, Konfiguration, Known issues aktualisieren. Dazu gehören die veraltete Statuszeile oben und Known issue 10 zu `MAIL_*`.
   - `documentation/*.md` auf den neuen Stand.
   - **Neue Anleitung `documentation/modules.md`:** „Ein Modul anlegen“ Schritt für Schritt mit einem vollständigen Mini-Beispiel als Code-Block (Router, Schemas, Service, CRUD, Model, Migration, Task, Admin-View, Tests). Das Beispiel steht nicht als Ordner im Repo.
   - `AGENTS.md` auf etwa 200 Zeilen kürzen, die *(target)*-Markierungen entfernen.
   - Die ADR-Index-Notiz „code does not match yet“ entfernen.
   - Dieses Dokument und `README.md` im Review-Ordner auf „abgeschlossen“ setzen.

## 7. Danach und Sammelpunkte

**Offene Entscheidungen** D2–D4, D8–D10 und D12 einzeln mit dem Owner besprechen (Empfehlung mit Begründung, der Owner entscheidet). Danach folgen die Review-Phasen 2–6 aus [README.md](README.md#4-umsetzungsplan):
- Phase 2: Unit of Work, UUIDv7, neue Migrations-Baseline, absolute Session-Laufzeit, Constraint-Namen.
- Phase 3: `must_change` durchsetzen (Known issue 16), `revoke_reason`.
- Phase 4: Rate-Limiting D4, das auch Known issue 17 löst.
- Phase 5: Tracing.

**Deploy-Runde** (deploy-engineer; `deployment/` ist git-ignored):
- Traefik schneidet heute `/api` ab und muss `/api/v1` unverändert weiterleiten. **Das blockiert jeden Deploy des neuen Vertrags.**
- `FORWARDED_ALLOW_IPS` auf die Traefik-Adressen setzen, nie `*`. `CORS_ORIGINS` auf das echte Frontend setzen.
- `/metrics` und `/health/ready` nicht öffentlich routen (Known issue 19).
- `prometheus.yml` auf `metrics_path: /metrics` umstellen.
- `DEBUG` aus dem Stack entfernen.
- Kompression (GZip) und HSTS in Traefik.
- Swarm-Postgres auf 18 (heute 16), `psycopg[c]`, Readiness-Probes.

**Bewusst akzeptiert:** Known issue 18 (`email_taken` verrät beim E-Mail-Wechsel, ob eine Adresse registriert ist). Der Export in der Admin-User-Ansicht bleibt eingeschaltet, und die Platzhalter-Felder in `UserResponse` (roles, permissions, preferences, profile_picture_url) bleiben vorerst.

## 8. Verifikation (nach jedem Schritt, Befehle aus `backend/`)

```bash
uv run pytest                                                          # volle Suite, Coverage-Gate 85, braucht Docker
DOCKER_HOST=tcp://127.0.0.1:1 uv run pytest tests/unit --no-cov -p no:cacheprovider   # Unit ohne Docker (bash)
uv run ruff check src tests run.py alembic                             # tests/ erst nach S3.2 grün
uv run ruff format --check src tests run.py alembic
uv run mypy                                                            # strict, Pfade aus [tool.mypy]
uv audit --frozen
```

Bei Migrationen kommt dazu: `alembic upgrade head`, `downgrade -1`, erneut `upgrade head` und `alembic check`, auf einem Wegwerf-Compose-Projekt und nur mit `DATABASE_URL` gesetzt, ohne `ENVIRONMENT` und ohne Secrets. Wenn Docker Desktop nicht läuft (z. B. nach einem Neustart), zuerst `Docker Desktop.exe` starten und warten, bis `docker info` antwortet.
