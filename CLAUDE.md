@AGENTS.md

## Claude Code specifics

- Project agents are defined in the repository root under `../.claude/agents/`. Prefer them over the generic agent: `backend-engineer`, `db-engineer`, `backend-tester`, `security-auditor`, `observability-engineer`, `deploy-engineer`, `docs-writer`, `researcher`. Frontend work uses `frontend-engineer`, `frontend-tester`, `design-reviewer`.
- Use plan mode for anything that touches `src/app/modules/auth/`, `src/app/core/`, `Dockerfile` or `deployment/`.
- Research and exploration subagents run on a cheaper model (`model: sonnet`); only `security-auditor`, `backend-engineer`, `db-engineer` and `deploy-engineer` default to Opus.
- Show every change to Claude Code settings files to the user before applying it.
- Open product decisions are listed in README → Open decisions; do not resolve them silently in code.
