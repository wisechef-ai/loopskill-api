# AGENTS.md — Recipes API contributor guide for AI agents

This file describes the repo layout, auth flow, and toolchain conventions for
AI coding agents (Codex, Claude Code, etc.) working on `wisechef-ai/recipes-api`.

---

## Module layout (post-secfix_1905/E)

```
app/
├── main.py                  FastAPI app factory (create_app) + lifespan hooks
├── config.py                Pydantic-settings; boot-time secrets gate for prod
├── auth_ctx.py              AuthContext frozen dataclass — single source of truth
│                            for scope, user_id, api_key_id, cookbook_scope, tier
├── authz.py                 Authorization predicates:
│                              can_install(ctx, skill)
│                              can_read_skill(ctx, skill)
│                              can_write_cookbook(ctx, cookbook)
│                              can_run_sandbox(ctx)
│                              can_call_admin_mcp_tool(ctx)
├── middleware.py            APIKeyMiddleware → populates request.state.auth_ctx
├── models.py                SQLAlchemy ORM models
├── schemas.py               Pydantic request/response schemas
│
│   — Route modules (post-Phase-E split) —
├── health_routes.py         GET /healthz + GET /api/healthz
├── skill_routes.py          GET /api/skills/* (search, trending, detail, related, external)
├── recipe_routes.py         GET /api/recipes/{slug} + GET /api/api-library/{slug}
├── install_routes.py        GET /api/skills/install + GET /api/skills/_download
├── access_routes.py         GET /api/skills/access + TIER_RANK + TIER_INSTALL_LIMITS
├── utm_redirects.py         /x/, /li/, /ig/, /yt/, /fb/ short-link redirectors
├── _skill_helpers.py        Shared helpers: _skill_to_out, _build_manifest, etc.
├── routes.py                Backward-compat re-exports only (≤80 lines)
│
│   — Feature modules —
├── auth_routes.py           OAuth (GitHub), JWT, cookie management
├── api_key_routes.py        CRUD for rec_ API keys
├── cookbook_routes.py       Cookbook CRUD + skill add/remove/sync
├── checkout_routes.py       Stripe checkout + billing portal
├── creator_routes.py        Creator payouts + Stripe webhooks
├── publisher_routes.py      Skill publish + tarball upload
├── admin_routes.py          Admin-only endpoints
│
│   — MCP server —
├── mcp/
│   ├── server.py            StreamableHTTP + stdio MCP server
│   ├── auth.py              MCP-specific key validation → AuthContext
│   └── tools/               One module per MCP tool
│       ├── install.py       recipes_install
│       ├── recipify.py      recipes_recipify
│       ├── recipes_sync.py  recipes_sync
│       └── …
│
│   — Sandbox —
├── sandbox/
│   ├── runner.py            SandboxRunner (firejail / bwrap backends)
│   ├── profile.py           SandboxProfile — network/fs/exec allow-lists
│   ├── domain_proxy.py      Allowlist-enforced CONNECT proxy
│   └── routes.py            POST /api/sandbox/run
│
│   — Utilities —
├── utils/
│   └── client_ip.py         Trusted-proxy-aware _real_client_ip()
└── last_used_tracker.py     Redis-batched last_used_at updates
```

---

## Auth flow

```
HTTP request
  → RateLimitMiddleware
  → APIKeyMiddleware.dispatch()
      ├── Master key path: hmac.compare_digest → AuthContext(scope="master")
      ├── Bearer JWT path: verify_jwt() → AuthContext(scope="user", user_id=…)
      └── x-api-key path: validate_key(db, key) → AuthContext(scope=…, tier=…)
           └─ request.state.auth_ctx = <AuthContext>
                └─ Route handler calls authz.can_*() before any data access
```

Every MCP tool receives the same `AuthContext` from `app/mcp/auth.py:validate_key`.

---

## Dev setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Install pre-commit hooks (runs on every git commit)
pip install pre-commit
pre-commit install
```

## Running tests

```bash
pytest -q                              # fast run
pytest --cov=app --cov-fail-under=85   # with coverage gate
```

## Linting (matches CI)

```bash
pre-commit run --all-files
```

Hooks:
| Hook | What it checks |
|------|----------------|
| `ruff --fix` | Import order, unused vars, BLE001 (blanket except), D103 (docstrings) |
| `ruff-format` | Code style |
| `bandit -r app/ -ll` | Security (severity ≥ MEDIUM) |
| `mypy --strict` | Type safety on 4 modules: auth_ctx, authz, middleware, utils/ |
| `actionlint` | GitHub Actions workflow syntax |
| `yamllint` | YAML in config/ |

## CI workflows

| File | Triggers | What it does |
|------|----------|--------------|
| `.github/workflows/ci.yml` | push/PR to main | pytest, alembic validate, coverage ≥85% |
| `.github/workflows/lint.yml` | push/PR to main | pre-commit run --all-files + pip-audit + safety |
| `.github/workflows/deploy.yml` | push to main (after CI green) | rsync + systemd restart |

## Key rules for agents

1. **No new logic in Phase-G scope** — hygiene only (docstrings, comments, toolchain).
2. **`except Exception:` requires `# Rationale: <reason>` on the preceding line** — enforced by BLE001 + Rationale convention. If you add a new blanket catch, justify it inline.
3. **mypy --strict scope is ONLY**: `app/auth_ctx.py`, `app/authz.py`, `app/middleware.py`, `app/utils/`. Broader mypy errors → file a tracking issue, don't expand scope.
4. **Before editing a god node** (APIKeyMiddleware.dispatch, validate_key, recipes_install, SandboxRunner.run, scan_tarball) run `gitnexus_impact` and confirm blast radius.
5. **One PR per phase**. Never modify `.coveragerc` or coverage CI step — that's Phase F's domain.
6. **Production deploy target**: `wisechef-hq:/home/wisechef/wiserecipes-api/`. Systemd unit: `wiserecipes-api.service`.
