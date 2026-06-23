# Security Policy — recipes-api

## Reporting a vulnerability

Email **security@wisechef.ai** with a description of the issue, the affected
endpoint or component, and a reproduction if you have one. Do **not** open a
public GitHub issue for a security report.

We aim to acknowledge within 3 working days and to ship a fix or a mitigation
plan within 14 days for anything that exposes customer data, allows auth
bypass, or permits remote code execution.

## What is in scope

`recipes-api` is the backend for [recipes.wisechef.ai](https://recipes.wisechef.ai).
In scope: the FastAPI app under `app/`, the MCP server under `app/mcp/`, the
skill sandbox under `app/sandbox/`, and the CI/deploy pipeline.

Out of scope: the public meta-skill repo
([wisechef-ai/recipes-skill](https://github.com/wisechef-ai/recipes-skill)),
third-party dependencies (report those upstream — Dependabot tracks them
here), and anything requiring a compromised developer machine.

## Security posture (how the codebase defends itself)

These controls are already in place; understanding them helps you assess a
finding's real impact:

- **Boot-time secrets gate.** `app/config.py` refuses to start in a non-sqlite
  (production) environment if `API_KEY`, `SIGNING_SECRET`, `JWT_SECRET`, or
  `HEARTBEAT_PEPPER` still hold a default `change-me` value.
- **Centralised authorization.** Every route resolves an `AuthContext`
  (`app/auth_ctx.py`) via `APIKeyMiddleware`, and calls an `authz.can_*`
  predicate (`app/authz.py`) before touching data. Auth is not re-implemented
  per route.
- **Signed install URLs.** Tarball-download URLs are `URLSafeTimedSerializer`
  tokens signed with `salt="recipes-skill-install"`; the salt is pinned by
  `tests/test_install_url_salt_consistency.py` so a producer cannot silently
  drift off it.
- **Skill discipline lint.** `scripts/skill_quality_gate.py` scans every
  published skill for leaked credentials, hostnames, and user names; the CI
  `skill-quality-gate-self-test` job proves the gate still blocks a leaky
  skill on every push.
- **Sandboxed skill execution.** Skill install scripts run under `firejail`
  or `bwrap` with an allow-listed egress proxy (`app/sandbox/`).
- **Static analysis in CI.** `bandit` (severity ≥ medium), `pip-audit`, and
  `safety` run on every PR via `.github/workflows/lint.yml`.

## Branch protection

`main` is the production branch — every push to it auto-deploys. Branch
protection (required CI + required review) depends on the GitHub plan for the
`wisechef-ai` org. Where it is not enforced at the platform level, the
contributor rules in `CONTRIBUTING.md` and the `CODEOWNERS` review routing
are the operative controls. Do not push directly to `main`.

## Supported versions

Only the current `main` is supported. There are no tagged releases — the
deployed version is whatever `main` last shipped.
