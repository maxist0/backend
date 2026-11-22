---
status: accepted
date: 2026-11-22
decision-makers: project owner
---

# Type checking: mypy and pyright strict are both gates, ty is informational

Resolves open decision D6.

## Context and Problem Statement

Open decision D6 asked which type checker the backend uses. The review plan (`documentation/review-2026-11/README.md`) recorded the answer "D6 = mypy strict als Gate + ty advisory" on 2026-11-16 and recommended no pyright as a third gate.

The style pass then enabled mypy strict for `src` and `tests` (S3.1, commit `ff48984`). On 2026-11-18 the owner added pyright as a second strict gate (S3.2, commit `6d01f6d`). The reason: the owner's VS Code runs Pylance, which is pyright, in strict mode, and it reported findings that mypy did not. A change that passed `uv run mypy` could therefore still show errors in the editor.

Which checkers must pass before a change is done, and what is `ty` for?

## Decision Drivers

* The command line and the editor must agree: what Pylance flags, a gate must flag too.
* The two checkers differ in inference and in how they treat some library stubs, so each finds things the other misses.
* A gate must be stable: pinned versions, no noise from pre-release tools.
* A suppression must stay reviewable.

## Considered Options

* mypy strict as the only gate, `ty` advisory (the review plan's answer).
* pyright strict as the only gate.
* mypy strict and pyright strict as gates, `ty` informational.
* `ty` as a gate.

## Decision Outcome

Chosen option: "mypy strict and pyright strict as gates, `ty` informational", because it is the only option in which the command line and the owner's editor report the same findings without dropping mypy's checks.

* `uv run mypy` is a gate. Configuration: `[tool.mypy]` in `pyproject.toml`, `strict = true`, `files = ["src", "tests"]`, plus the error codes `ignore-without-code` and `deprecated`.
* `uv run pyright` is a gate. Configuration: `[tool.pyright]` in `pyproject.toml`, `typeCheckingMode = "strict"`, covering `src`, `tests`, `run.py` and `alembic/env.py`. Pylance reads the same section.
* `uv run ty check` is informational only. It is not a gate and its findings do not block a change.
* Both gates are in the Definition of done in [AGENTS.md](../../AGENTS.md#definition-of-done) and in the commands in the [README](../../README.md#commands).

### Consequences

* Good, because the editor and `uv run pyright` agree, so a clean CI-style run means a clean editor.
* Good, because two checkers catch more than one.
* Good, because a finding that only one tool reports is still a real finding to fix, not a reason to weaken either tool.
* Bad, because every change must satisfy two checkers, and a construct that is fine for one may need a suppression for the other.
* Bad, because a suppression must name its rule or code and give its reason on the same line: `# type: ignore[code]  # reason` for mypy, `# pyright: ignore[rule] - reason` for pyright. Reviewers read these lines.
* Neutral, because pyright is pinned exactly (`pyright[nodejs]==1.1.414`, like ruff) so a new release is an explicit change, not a side effect of a relock. The `nodejs` extra brings Node.js as a locked wheel instead of a download at run time.
* Neutral, because Pylance finds `backend/pyproject.toml` when the workspace is opened through `Prooduction.code-workspace`, a multi-root workspace with the folders `backend`, `frontend` and the workspace root (added 2026-11-22). Opening only the workspace root would not apply the backend configuration to files in `backend/`.
* Neutral, because `ty` stays in the `lint` group (`ty>=0.0.104`) and can be promoted by a later ADR once it is stable.

## Pros and Cons of the Options

### mypy strict only

* Good, because it is the smallest setup and mypy is mature.
* Bad, because it did not report what the owner's strict Pylance reported, so editor and command line disagreed.

### pyright strict only

* Good, because it matches Pylance exactly.
* Bad, because it drops the checks mypy already performs and the existing `# type: ignore[code]` convention.

### ty as a gate

* Good, because it is fast.
* Bad, because it is a pre-release tool (version 0.0.x), so its findings and rules still change between releases.

## More Information

* Commits: `ff48984` (mypy strict, ruff 0.17.5, style pass), `6d01f6d` (typed tests, pyright strict, `pyrightconfig.json` folded into `pyproject.toml`).
* Workspace file: `Prooduction.code-workspace` at the workspace root.
* Conventions: [AGENTS.md → Code style](../../AGENTS.md#code-style).
