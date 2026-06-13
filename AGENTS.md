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

---

## Cookbook share-tokens (2026-05-21 update)

> See [docs/share-tokens.md](docs/share-tokens.md) for the public-facing guide.

Share tokens (`cbt_<8hex>_<32hex>`) let a recipient install the skills of one specific cookbook without owning the recipient agent's API keys. Authz is centralised:

- `AuthContext(scope="cbt_token", cookbook_scope=<uuid>)` is stamped by `app/middleware.py` for any `x-api-key: cbt_*` header.
- Middleware hard-restricts cbt_ tokens to `/api/cookbooks/*` paths and any `/_publish` → `403` regardless of scope.
- `app/authz.py:can_read_skill` has a 4th clause that resolves cookbook-scope authority via the `CookbookSkill` join — so `can_install(ctx, skill, db=db)` returns True iff the skill is in `ctx.cookbook_scope`. The `db` parameter MUST be threaded by callers in private-skill paths; the predicate fails closed without it.
- `_resolve_owned_cookbook` (cookbook_routes.py) accepts ownership via `ctx.cbt_cookbook_id == cb.id` in addition to the existing user/master paths.

Scope vocabulary: `{read, edit, install}`. Default for new tokens is `install` (server-side `DEFAULT 'install'`, see migration `d8c8a3f721ec_cookbook_share_install_scope.py`). Existing tokens keep their stored scope — no auto-upgrade.

MCP entry point: `app/mcp/tools/cookbook_install.py:recipes_cookbook_install(db, ctx, cookbook_id=None, slug=None)`. cbt_token callers may omit `cookbook_id` (defaults to `ctx.cookbook_scope`). Single-skill payload mirrors `recipes_install`; bulk payload mirrors `POST /api/cookbooks/{id}/install`.

**When adding a new cookbook route, you MUST:**
1. Call `_enforce_cbt_scope_for_cookbook_route(request, cookbook_id)` to gate scope.
2. Use `_resolve_owned_cookbook(db, ctx, cookbook_id)` for ownership (handles the cbt_ branch).
3. Pass `db=db` to any `authz.can_read_skill` / `authz.can_install` call.

**Salt-parity discipline:** any new signed-URL producer (cookbook install URL, single-skill install URL, future variants) MUST use salt `recipes-skill-install` so it verifies against `install_routes._download`. Add it to the regression suite in `test_secfix_1905_d_cookbook_install_url.py`. Don't ship a salt-drifting signer.

---

## Install provenance + feedback routing (spotify_0608 Ph E)

> See `app/services/provenance.py` for the single seam.

Every install transport returns a `provenance_id` — a RANDOM, server-stored opaque token (`secrets.token_urlsafe(32)`) mapping → `install_event_id`. The token carries ZERO client-readable metadata (this is deliberate: a *signed* payload via `itsdangerous` is signed-but-not-encrypted, so embedding `cookbook_id`/`skill_id` would leak them). Resolution is a pure server-side join: `provenance_id → ProvenanceRecord → InstallEvent → (cookbook_id, skill_id, version_semver, attribution)`.

**When adding a NEW install path, you MUST:**
1. Call `app.services.provenance.record_install_with_provenance(db, skill=..., version_semver=..., request=..., source=..., cookbook_id=..., attribution=...)` — it records the `InstallEvent`, bumps the denormalised counter with the SAME `is_test` integrity rule as `_record_install_event` (Ph B §4.2), stamps `cookbook_id` + `attribution`, and mints the provenance row in one transaction. Do NOT hand-roll an `InstallEvent` insert.
2. Return `provenance_id` in the response envelope. In BULK envelopes it rides PER-SKILL under `skills[]`, never cookbook-top-level (R4 contract).
3. `attribution='attributed'` when you fetched a real body (you know skill+version); `attribution='unattributed'` for honest deep-link / non-fetch installs (no body → no deeper attribution). A TRANSIENT fetch failure is NOT unattributed — it stays a hard error and never reaches the recorder.

**Feedback routing is provenance-deterministic.** `recipes_feedback` and `recipes_report_skill_error` accept `provenance_id`; the server resolves it to the EXACT cookbook the install came from and routes the issue to that cookbook's configured curator repo (`route_targets_for_provenance`). The old "first cookbook the user owns with a repo set" guess is DELETED — without a `provenance_id`, routing falls back to the default repo (no guessing). PAT path is live; GitHub App is a distinct future substream (`mode='github_app'` raises until registered).


---

## Cookbook → skill-bundle bridge (well-known, 2026-06-13)

`app/cookbook_wellknown_routes.py` serves a public cookbook as an
agentskills.io bundle so any agent can install it natively:

```
hermes skills install well-known:https://recipes.wisechef.ai/api/cookbooks/public/<slug>
```

Two PUBLIC routes under `/api/cookbooks/public/` (already in `PUBLIC_PREFIXES`):
- `.../{slug}/.well-known/skills/index.json` — lists all skills; paid flagged `locked`.
- `.../{slug}/.well-known/skills/{skill}/SKILL.md` — FREE → real `readme`; PAID → non-leaking stub.

**Paywall invariant:** paid `readme` body never crosses this unauthenticated
surface (tested in `tests/test_cookbook_wellknown.py::test_paid_skill_serves_stub_not_body`).
This is the SERVE half of the federation `well-known` adapter (which CONSUMES).
Vault: `shared-knowledge/recipes/cookbook-bundle-bridge.md`.
