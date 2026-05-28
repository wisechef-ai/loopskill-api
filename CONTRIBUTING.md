# Contributing to recipes-api

This is the backend for [recipes.wisechef.ai](https://recipes.wisechef.ai).
It is worked by AI agents (Codex, Claude Code) and humans alike — `AGENTS.md`
has the module map and auth-flow diagram; this file has the workflow.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install pre-commit && pre-commit install
```

## The golden rule: `main` is production

Every push to `main` triggers `.github/workflows/deploy.yml`, which rsyncs
to `wisechef-hq` and restarts `wiserecipes-api`. There is no staging branch.
**Never push directly to `main`** — always go through a PR.

## Workflow

1. **Branch.** `agent/<name>/<short-topic>` or `fix/<topic>`. For isolated
   parallel work use a git worktree (`git worktree add ...`).
2. **Write the test first** where it applies. A bug fix without a regression
   test will be sent back.
3. **Run the suite locally.** `pytest -q`. The full suite must be green
   before you open the PR — CI enforces it (see below).
4. **Lint.** `pre-commit run --all-files` — matches the CI `lint` job
   (`ruff`, `ruff-format`, `bandit`, `mypy --strict` on the auth modules,
   `actionlint`, `yamllint`).
5. **Open the PR.** Fill in the PR template. One logical change per PR.
6. **Wait for CI green**, then squash-merge. `delete_branch_on_merge` is on
   — the branch cleans itself up.
7. **You merged it, you ship it** for any change that needs more than the
   auto-deploy (e.g. a portal rebuild). Don't hand the deploy to someone else.

## CI gates (`.github/workflows/ci.yml`)

| Job | What it enforces |
|-----|------------------|
| `pytest + coverage gate` | Full `tests/` suite green **and** line coverage ≥ the `--cov-fail-under` threshold. The threshold is a **ratchet** — raise it when coverage rises, never lower it. |
| `Quality gate self-test` | `scripts/skill_quality_gate.py` still blocks a leaky skill and passes a clean one. |
| `lint` | `pre-commit` + `pip-audit` + `safety`. |
| `Self-leak check` | Informational scan of `scripts/` for leaked credentials. |

A hung test fails fast: the pytest job has a 20-minute cap and a 120s
per-test `pytest-timeout`.

## Conventions

- **Conventional commits.** `fix(...)`, `feat(...)`, `test(...)`, `chore(...)`,
  `ci(...)`, `refactor(...)`.
- **`except Exception:` needs a reason.** Put `# Rationale: <why>` on the
  line above — `ruff` BLE001 + this convention is enforced.
- **`mypy --strict` scope is fixed**: `app/auth_ctx.py`, `app/authz.py`,
  `app/middleware.py`, `app/utils/`. A broader mypy error → file a tracking
  issue, don't widen the scope in your PR.
- **God nodes.** Before editing `APIKeyMiddleware.dispatch`, `validate_key`,
  `recipes_install`, `SandboxRunner.run`, or `scan_tarball`, understand the
  blast radius first — these are reviewed by `CODEOWNERS`.
- **Tier slugs are a contract.** Canonical DB slugs are `free`, `pro`, `pro_plus`.
  Legacy aliases `cook`/`operator`/`studio` are accepted as 30-day read aliases
  until 2026-06-10. Display labels and prices live in `config/tiers.yaml` — edit only there.
- **Migrations.** One alembic head. After merging two PRs that each added a
  migration, run `alembic merge` and commit the merge revision.

## Deploying

`main` auto-deploys via CI. After the deploy run goes green, sanity-check
production:

```bash
curl -s https://recipes.wisechef.ai/api/healthz   # {"status":"ok",...}
```

To deploy an API change by hand (rare — CI does it), or to recover a
failed deploy, see the `recipes-marketplace-deploy` operational runbook.

## Reporting security issues

Do not open a public issue. See `SECURITY.md`.
