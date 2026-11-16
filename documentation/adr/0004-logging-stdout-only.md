---
status: accepted
date: 2026-11-16
decision-makers: project owner
---

# Logging to stdout only

## Context and Problem Statement

The app exported logs to an OpenTelemetry collector with an in-process OTLP handler. Problems with that setup:

* The OTLP log handler is deprecated in the SDK.
* It produced duplicate records.
* It exported the log body as a Python `repr` string instead of structured data.
* uvicorn errors were swallowed.

Where does the app send its logs?

## Decision Drivers

* Logs must be complete, structured and free of personal data.
* The app should not depend on an experimental SDK signal.
* Twelve-factor: logs are an event stream on stdout.

## Considered Options

* stdout only; a collector reads the container output.
* Keep the in-app OTLP export.

## Decision Outcome

Chosen option: "stdout only", because the collector can ship container output without any code in the app.

* The app writes logs to stdout: console renderer in dev, JSON otherwise.
* The app has no OTLP log export. A collector such as Alloy reads container stdout. The `OTLP_*` settings are removed.
* Tracing is a later phase and is decided separately.
* `log_unexpected_error` remains the way to log unexpected exceptions.
* Log events are named `area.action` and carry no PII.

### Consequences

* Good, because there is one log path, so no duplicate records, and uvicorn output stays visible.
* Good, because the app no longer depends on a deprecated SDK handler.
* Good, because JSON on stdout keeps the fields structured.
* Bad, because the deployment needs a collector that reads container stdout (the monitoring stack already has Alloy).
* Bad, because log-to-trace correlation waits for the tracing phase. `request_id` is the correlation key until then.

## Pros and Cons of the Options

### Keep in-app OTLP

* Good, because the app would push logs without a collector reading stdout.
* Bad, because of the four problems above, and OpenTelemetry logs are still a development signal.

## More Information

* Conventions: [AGENTS.md → Security rules](../../AGENTS.md#security-rules) (what never to log).
* Implemented in S1–S3 of the 2026-11 cleanup.
