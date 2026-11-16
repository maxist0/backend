---
status: accepted
date: 2026-11-16
decision-makers: project owner
---

# Test split: unit and integration

## Context and Problem Statement

Every backend test needed Docker, because the suite was one set of integration tests. A change to pure logic still cost a container start, so the feedback loop was slow and the suite could not run where Docker is unavailable.

How is the suite organised so that pure logic is tested fast and persistence is tested for real?

## Decision Drivers

* A fast feedback loop for pure logic.
* Real Postgres and Redis for anything that touches persistence (no mocked database).
* No duplicate coverage of the same journey at several levels.

## Considered Options

* Two folders: `tests/unit/` without Docker and `tests/integration/` with Testcontainers.
* Add a backend e2e folder on top of the two.
* Keep one suite.

## Decision Outcome

Chosen option: "two folders, unit and integration", because the rule "needs no DB or Redis means unit" is simple to apply.

* `tests/unit/` needs no Docker and runs in seconds: `uv run pytest tests/unit` (add `--no-cov` for a run without the coverage gate, which applies only to the full run).
* `tests/integration/` uses `TestClient` with Postgres and Redis Testcontainers.
* Layout: `tests/unit/{core,modules/<name>}` and `tests/integration/{core,contract,modules/<name>,admin}`. A new module brings its own `conftest.py` and never edits the root one.
* There is no backend e2e folder. End-to-end journeys are covered by frontend Playwright.
* Do not test third-party libraries. Guard our own configuration of them.

### Consequences

* Good, because the unit run finishes in seconds and needs no Docker.
* Good, because integration tests keep real database behaviour.
* Good, because modules stay self-contained in their tests.
* Bad, because the existing tests must be sorted into the two folders (part of S1–S3 of the 2026-11 cleanup).
* Bad, because the coverage gate needs the full run, so a unit-only run cannot show it.

## Pros and Cons of the Options

### Backend e2e folder

* Good, because it would exercise the deployed stack.
* Bad, because frontend Playwright already covers the user journeys end to end, so it would duplicate that at higher cost.

### One suite

* Good, because there is nothing to reorganise.
* Bad, because pure-logic tests keep paying for Docker.

## More Information

Conventions: [AGENTS.md → Testing](../../AGENTS.md#testing-target).
