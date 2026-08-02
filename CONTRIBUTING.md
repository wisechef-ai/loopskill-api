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
to the production host and restarts the API service. There is no staging branch.
**Never push directly to `main`** — always go through a PR.

## Workflow

1. **Branch.** `agent/<name>/<short-topic>` or `fix/<topic>`. For isolated
   parallel work use a git worktree (`git worktree add ...`).
2. **Write the test first** where it applies. A bug fix without a regression
   test will be sent back.
3. **Run the suite locally.** `pytest -q`. The full suite must be green
   before you open the PR — CI enforces it (see below).
4. **Lint.** `ruff check app/ && ruff format --check app/` — matches the CI
   `lint (ruff)` job. `bandit -r app/ -lll` and `pip-audit -r requirements.txt`
   match the CI `lint.yml` gates. `pre-commit run --all-files` runs a broader
   local hook set (`mypy --strict` on the auth modules, `actionlint`,
   `yamllint`) that is not currently wired into CI — see `AGENTS.md`.
5. **Open the PR.** Fill in the PR template. One logical change per PR.
6. **Wait for CI green**, then squash-merge. `delete_branch_on_merge` is on
   — the branch cleans itself up.
7. **You merged it, you ship it** for any change that needs more than the
   auto-deploy (e.g. a portal rebuild). Don't hand the deploy to someone else.

## CI gates (`.github/workflows/ci.yml`)

| Job | What it enforces |
|-----|------------------|
| `lint (ruff)` (`ci.yml`) | `ruff check` and `ruff format --check` on `app/`. |
| `pytest + coverage` (`ci.yml`) | Full `tests/` suite green **and** line coverage ≥ the `--cov-fail-under` threshold. The threshold is a **ratchet** — raise it when coverage rises, never lower it. |
| `secret scan (gitleaks)` (`ci.yml`) | Working-tree scan for leaked credentials via `.gitleaks.toml`. |
| `bandit` (`lint.yml`) | `bandit -r app/ -lll` — HIGH severity only. MEDIUM findings exist and are not yet gated (see `lint.yml` header). |
| `pip-audit` (`lint.yml`) | Dependency vulnerability scan against `requirements.txt`. |

A hung test fails fast: the pytest job has a 20-minute cap and a 120s
per-test `pytest-timeout`.

## Conventions

- **Conventional commits.** `fix(...)`, `feat(...)`, `test(...)`, `chore(...)`,
  `ci(...)`, `refactor(...)`.
- **`except Exception:` needs a reason.** Put `# Rationale: <why>` on the
  line above — `ruff` BLE001 + this convention is enforced.
- **`mypy --strict` scope is fixed**: `app/auth_ctx.py`, `app/authz.py`.
  A broader mypy error → file a tracking issue, don't widen the scope in
  your PR. (Enforced only via local pre-commit — not wired into CI.)
- **God nodes.** Before editing `APIKeyMiddleware.dispatch`, `validate_key`,
  `loopskill_install`, `SandboxRunner.run`, or `scan_tarball`, understand the
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
