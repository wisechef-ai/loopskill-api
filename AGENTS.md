# AGENTS.md — LoopSkill API contributor guide for AI agents

This file describes the repo layout, auth flow, and toolchain conventions for
AI coding agents (Codex, Claude Code, etc.) working on `wisechef-ai/loopskill-api`
(repo renamed from `wisechef-ai/recipes-api`; GitHub auto-redirects the old name).

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
├── middleware/               APIKeyMiddleware → populates request.state.auth_ctx
│   ├── api_key.py            APIKeyMiddleware.dispatch, rec_/cbt_/rec_fleet_ key validation
│   ├── bundle_routing.py     cbt_ token path-scoping (cookbook routes only)
│   ├── rate_limit.py         RateLimitMiddleware
│   └── _token_auth.py        Shared JWT/master-key comparison helpers
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
│       ├── install.py          loopskill_install
│       ├── recipify.py         loopskill_skillify
│       ├── loopskill_sync.py   loopskill_sync
│       ├── bundle_install.py   loopskill_bundle_install
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
ruff check app/ && ruff format --check app/    # matches ci.yml's lint job
bandit -r app/ -lll                            # matches lint.yml's bandit gate (HIGH only)
pip-audit -r requirements.txt                  # matches lint.yml's pip-audit gate
```

`pre-commit run --all-files` runs a broader local hook set (below) but only
`ruff --fix`/`ruff-format` and `bandit -lll` are actually wired into CI today.

Hooks (local `pre-commit install`, NOT all enforced in CI — see CI workflows table):
| Hook | What it checks | Enforced in CI? |
|------|----------------|------------------|
| `ruff --fix` | Import order, unused vars, BLE001 (blanket except), D103 (docstrings) | Yes — `ci.yml` lint job |
| `ruff-format` | Code style | Yes — `ci.yml` lint job |
| `bandit -lll` | Security (severity = HIGH only) | Yes — `lint.yml` |
| `mypy --strict` | Type safety on 2 modules: `app/auth_ctx.py`, `app/authz.py` | No — local pre-commit only |
| `actionlint` | GitHub Actions workflow syntax | No — local pre-commit only |
| `yamllint` | YAML in config/ | No — local pre-commit only |

## CI workflows

| File | Triggers | What it does |
|------|----------|--------------|
| `.github/workflows/ci.yml` | push/PR to main | `ruff check` + `ruff format --check`, pytest + coverage ≥80%, gitleaks secret scan |
| `.github/workflows/lint.yml` | push/PR to main | `bandit -r app/ -lll` (HIGH only) + `pip-audit` |
| `.github/workflows/deploy.yml` | push to main (after CI green) | self-hosted runner: git pull + pip install + alembic upgrade + systemd restart + public verification |

## Key rules for agents

1. **No new logic in Phase-G scope** — hygiene only (docstrings, comments, toolchain).
2. **`except Exception:` requires `# Rationale: <reason>` on the preceding line** — enforced by BLE001 + Rationale convention. If you add a new blanket catch, justify it inline.
3. **mypy --strict scope is ONLY**: `app/auth_ctx.py`, `app/authz.py`. Broader mypy errors → file a tracking issue, don't expand scope. (Note: this scope is enforced only via local pre-commit — it is not wired into any CI workflow.)
4. **Before editing a god node** (APIKeyMiddleware.dispatch, validate_key, loopskill_install, SandboxRunner.run, scan_tarball) run `gitnexus_impact` and confirm blast radius.
5. **One PR per phase**. Never modify `.coveragerc` or coverage CI step — that's Phase F's domain.
6. **Production deploy target**: your own host (see deploy.yml). This is open-core — self-host anywhere.

---

## Cookbook share-tokens (2026-05-21 update)

> See [docs/share-tokens.md](docs/share-tokens.md) for the public-facing guide.

Share tokens (`cbt_<8hex>_<32hex>`) let a recipient install the skills of one specific cookbook without owning the recipient agent's API keys. Authz is centralised:

- `AuthContext(scope="cbt_token", cookbook_scope=<uuid>)` is stamped by `app/middleware.py` for any `x-api-key: cbt_*` header.
- Middleware hard-restricts cbt_ tokens to `/api/cookbooks/*` paths and any `/_publish` → `403` regardless of scope.
- `app/authz.py:can_read_skill` has a 4th clause that resolves cookbook-scope authority via the `CookbookSkill` join — so `can_install(ctx, skill, db=db)` returns True iff the skill is in `ctx.cookbook_scope`. The `db` parameter MUST be threaded by callers in private-skill paths; the predicate fails closed without it.
- `_resolve_owned_cookbook` (cookbook_routes.py) accepts ownership via `ctx.cbt_cookbook_id == cb.id` in addition to the existing user/master paths.

Scope vocabulary: `{read, edit, install}`. Default for new tokens is `install` (server-side `DEFAULT 'install'`, see migration `d8c8a3f721ec_cookbook_share_install_scope.py`). Existing tokens keep their stored scope — no auto-upgrade.

MCP entry point: `app/mcp/tools/bundle_install.py:loopskill_bundle_install(db, ctx, cookbook_id=None, slug=None)`. cbt_token callers may omit `cookbook_id` (defaults to `ctx.cookbook_scope`). Single-skill payload mirrors `loopskill_install`; bulk payload mirrors `POST /api/cookbooks/{id}/install`.

**When adding a new cookbook route, you MUST:**
1. Call `_enforce_cbt_scope_for_cookbook_route(request, cookbook_id)` to gate scope.
2. Use `_resolve_owned_cookbook(db, ctx, cookbook_id)` for ownership (handles the cbt_ branch).
3. Pass `db=db` to any `authz.can_read_skill` / `authz.can_install` call.

**Salt-parity discipline:** the canonical salt is `loopskill-install`. `app/install_routes.py:_verify_signed_token` tries `loopskill-install` FIRST; the legacy salt `recipes-skill-install` is accepted only as a compat fallback for pre-rename in-flight URLs still circulating when this rolled out. All producers now sign with `loopskill-install` — any new signed-URL producer (cookbook install URL, single-skill install URL, future variants) MUST use salt `loopskill-install` so it verifies against `install_routes._download`. Add it to the regression suite in `test_secfix_1905_d_cookbook_install_url.py`. Don't ship a salt-drifting signer, and don't sign with the legacy salt — it exists only so the verifier keeps accepting URLs minted before the cutover.

---

## Install provenance + feedback routing (spotify_0608 Ph E)

> See `app/services/provenance.py` for the single seam.

Every install transport returns a `provenance_id` — a RANDOM, server-stored opaque token (`secrets.token_urlsafe(32)`) mapping → `install_event_id`. The token carries ZERO client-readable metadata (this is deliberate: a *signed* payload via `itsdangerous` is signed-but-not-encrypted, so embedding `cookbook_id`/`skill_id` would leak them). Resolution is a pure server-side join: `provenance_id → ProvenanceRecord → InstallEvent → (cookbook_id, skill_id, version_semver, attribution)`.

**When adding a NEW install path, you MUST:**
1. Call `app.services.provenance.record_install_with_provenance(db, skill=..., version_semver=..., request=..., source=..., cookbook_id=..., attribution=...)` — it records the `InstallEvent`, bumps the denormalised counter with the SAME `is_test` integrity rule as `_record_install_event` (Ph B §4.2), stamps `cookbook_id` + `attribution`, and mints the provenance row in one transaction. Do NOT hand-roll an `InstallEvent` insert.
2. Return `provenance_id` in the response envelope. In BULK envelopes it rides PER-SKILL under `skills[]`, never cookbook-top-level (R4 contract).
3. `attribution='attributed'` when you fetched a real body (you know skill+version); `attribution='unattributed'` for honest deep-link / non-fetch installs (no body → no deeper attribution). A TRANSIENT fetch failure is NOT unattributed — it stays a hard error and never reaches the recorder.

**Feedback routing is provenance-deterministic.** `loopskill_feedback` and `loopskill_report_skill_error` accept `provenance_id`; the server resolves it to the EXACT cookbook the install came from and routes the issue to that cookbook's configured curator repo (`route_targets_for_provenance`). The old "first cookbook the user owns with a repo set" guess is DELETED — without a `provenance_id`, routing falls back to the default repo (no guessing). PAT path is live; GitHub App is a distinct future substream (`mode='github_app'` raises until registered).


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

---

## fleetos_1607 — the control plane for AI agent fleets (2026-07-16)

LoopSkill grows from a marketplace into the **control plane for AI agent fleets**.
The desired state of a WHOLE agent — its loops (crons) with per-member
placements, scripts packs, SOUL, host profile, secret refs — is captured as
first-class declarative artifacts grouped into golden bundles. One primitive
serves three products: DR restore, host migration, and new-agent kickstart.

Public category descriptor is **"the control plane for AI agent fleets"**
("Kubernetes for X" / "Spotify for X" are third-party trademarks — allowed in
editorial body prose, BANNED from product name, hero, meta titles, ads).

Shipped phases (all additive, one PR per phase):

- **Phase 0** (`app/services/fleet_artifacts.py`, tables `loop_manifests` /
  `scripts_packs` / `host_profiles`): slim loop-manifest v1 (canonical
  serialization round-trips byte-identical), scripts-pack secret-scan gate
  (reuses `security_scan.scan_tarball`), host-profile compatibility validation.
  The `soul` artifact IS the existing `Personality` model (no new table).
- **Phase A** (`app/services/placement.py`, `app/mcp/tools/placement.py`, tables
  `loop_placements` / `placement_confirmations` / `fleet_member_liveness`):
  epoch-CAS placements — every transition is a compare-and-swap on a monotonic
  `placement_epoch`. Manager surface gated by `authz.can_manage_fleet` (a bare
  fleet-member key gets 403). Stale-member alert replaces the deleted failover.
  NO exactly-once claim (honest-guarantee doctrine).
- **Phase B** (`app/services/harvest.py`, `app/mcp/tools/harvest.py`): reverse
  GitOps — an agent's live state is diffed vs the golden bundle and proposed back
  through the EXISTING feedback rail (`feedback_repo` + Fernet PAT vault +
  `dispatch_issue`). Zero new tables/auth. Poisoned artifacts blocked pre-proposal.
- **Phase E** (`app/services/byo_origin.py`, tables `artifact_origins` /
  `origin_drift_events`): BYO-repo registries — the server stores an artifact's
  SHA-pinned origin + content-hash LOCK (metadata only, never the bytes); agents
  fetch content directly from the user's repo and verify against the lock,
  failing closed + recording origin-drift on mismatch. The hyperscale gate.
- **Phase D** (`app/services/run_registry.py`, additive columns on `loop_runs`):
  honest run registry — dedup on `(loop, tick, attempt, epoch)`, `unknown` is a
  first-class outcome, stale-epoch runs excluded from pass numerators. Pass rate =
  passes / (total − unknown − stale); honest None when nothing counts.
- **Phase T** (`app/fleet_skill_serve_routes.py`, `docs/fleet-skill/SKILL.md`):
  the trojan skill. `GET /fleet/skill` serves a complete fleet-control-plane
  SKILL.md (larrybrain pattern) — any agent becomes a fleet CLIENT in one curl.
  Public GET-only (in `EXEMPT_PATHS`); distinct from `/skill` (the marketplace
  skill). When adding a new delegated MCP tool, append its dispatcher to
  `app/mcp/dispatch_chain.py` (NOT to `server.py._dispatch` — the 600-line gate).

Plan-doc: `obsidian-vault/projects/recipes/plans/2026-07-16-fleetos-1607-execution-plan.md`.

---

## Legacy identifier deprecation windows (qa0208-w3)

The API lane of the cookbook→bundle / recipes→loopskill coordinated migration
(qa0208) added DUAL-ACCEPT aliases for every wire identifier that still had a
live legacy form: new canonical value is written/preferred, the legacy value
is accepted as a documented fallback so no existing client breaks. This
mirrors the install-URL salt pattern already established in
`app/install_routes.py` (`loopskill-install` tried first, `recipes-skill-install`
fallback — see "Cookbook share-tokens" above).

| Identifier | Canonical | Legacy fallback | Where the fallback lives |
|---|---|---|---|
| API route prefix | `/api/bundles/*` | `/api/cookbooks/*` | `app/bundle_routes.py` (`router.include_router` dual-mount), same for `bundle_deployment_routes.py` and `bundle_wellknown_routes.py`. cbt_ token path-scoping in `app/middleware/api_key.py` allow-lists both prefixes; `_publish` stays 403 on both. Pre-existing; regression-pinned by `tests/test_loopskill_bundle_surface_symmetry.py` and `tests/test_qa0208_dualaccept.py`. |
| Referral cookie | `loopskill_ref` | `recipes_ref` | `app/referral.py:resolve_referral_cookie()` reads canonical first, falls back to legacy; `app/auth_routes.py` OAuth callbacks call the resolver and delete BOTH cookie names on success. |
| Agent/CLI env var | `LOOPSKILL_API_KEY` | `RECIPES_API_KEY` | `tools/recipes_cli.py:_get_api_key()` and `recipes/recipes-cookbook-reconcile/scripts/_reconcile_lib/reconcile_cli.py:main()` — both check canonical env var first, fall back to legacy, never fail if only the legacy var is set. |
| Stripe customer/subscription/account metadata user key | `metadata.loopskill_user_id` | `metadata.wiserecipes_user_id` | `app/subscription_service.py` (`create_customer`, `create_checkout_session`, `downgrade_to_pro`) and `app/stripe_service.py` (`create_connect_account`) dual-WRITE both keys on every Stripe object creation/update. `app/subscription_service.py:_user_from_subscription_metadata()` reads canonical first, falls back to legacy — required so in-flight Stripe objects created before the rename (which only carry the legacy key) still resolve to a user on webhook delivery. |
| API key prefix | `lsk_` | `rec_` | `app/middleware/api_key.py` (`USER_KEY_PREFIXES = ("rec_", "lsk_")`, used by `_auth_ctx_from_api_key` and `APIKeyMiddleware.dispatch`) and `app/mcp/auth.py:validate_key`. **Minting still issues `rec_live_` keys** (`app/api_key_routes.py:KEY_PREFIX`) — switching the mint default is a separate follow-up, deliberately out of scope here because display truncation and prefix-format tests (`test_5b_backend.py`, `test_mcp_fleet.py`, etc.) currently assume `rec_live_`. This entry only widens the READ/validate path. |

All five are regression-pinned in `tests/test_qa0208_dualaccept.py`.

---

## Agent self-registration (agentreg_0819, 2026-08-19)

`POST /api/agents/register` is the FIRST key-minting path that does not require
a human. Every other mint goes through `app/api_key_routes.py:_require_user`,
which 401s without an OAuth session — so an autonomous agent arriving from
`llms.txt` or an MCP directory could browse the catalog and nothing else.

```
POST /api/agents/register            public (PUBLIC_POST_ONLY_PATHS), Ed25519 proof-of-key
POST /api/admin/agent-identities/{id}/revoke   master key only
GET  /api/admin/agent-identities               master key only
GET  /.well-known/agent.json         public (EXEMPT_PATHS)
GET  /.well-known/mcp.json           public (EXEMPT_PATHS)
```

**The canonical string** (`app/services/agent_registration.py`) — the client
signs the UTF-8 bytes of, verbatim:

```
loopskill-agent-register:v1:{pubkey}:{timestamp}:{nonce}:{agent_name}
```

It is stated in THREE places that must agree — the service, the route
docstring, and `/.well-known/agent.json` — and
`tests/test_agentreg_0819_agent_self_registration.py` pins all three against
each other. `.well-known` builds its copy by CALLING the service's builder, so
only the route docstring can drift. **If you change the format, bump
`CANONICAL_VERSION`** rather than editing v1: an old client's v1 string must
fail to verify against a v2 server, not silently sign less than it thinks.

**Identity model — a SHADOW `User` row.** `agent_identities.user_id` is a
UNIQUE FK to a `users` row minted at registration; the agent's `api_keys` hang
off that. This was chosen over hanging keys directly off `agent_identities`
because `api_keys.user_id` is NOT NULL and this codebase's master-key sentinel
is literally `is_master = (api_key_user_id is None)` — a nullable key owner is
one missing `is not None` away from an agent key reading as master. With the
shadow row, an agent key produces the SAME `AuthContext(scope="user",
tier=None)` a free human key does, so **`app/auth_ctx.py` and `app/authz.py`
were not touched by this feature.** The shadow row has no `github_id` /
`google_id` / `email`, so no OAuth flow can land on it.

**Key prefix `rec_agent_`** (`app/middleware/key_prefixes.py`) is a NARROWING
of the `rec_` user-key namespace, not a new one — it already satisfies
`USER_KEY_PREFIXES`, so `app/middleware/api_key.py` and `app/mcp/auth.py`
validate it unchanged. The prefix exists so both paths can cheaply spot an agent
key and apply the ONE extra gate it needs.

**When adding a new key-validation path, you MUST** call
`app.middleware._agent_identity.agent_key_is_blocked(db, user_id)` for any key
matching `is_agent_key(key)`. It fails CLOSED on a missing identity, a revoked
identity, and any lookup failure. It lives in one module precisely so REST and
MCP cannot drift — a REST-only revocation is not a revocation.

**Abuse walls** (all `WR_AGENT_REGISTRATION_*` in `app/config.py`): per-IP and
global rolling-24h enrolment caps, a ±5 min signed-timestamp window, and a
single-use nonce enforced by a UNIQUE constraint on
`agent_registration_nonces.nonce_hash`. The nonce store is DB-backed, NOT Redis:
`app.middleware.get_redis` degrades to `None` when Redis is unreachable, and a
replay wall that opens when the cache is down is not a wall.

A registered key is FREE tier by construction (the shadow user has NULL
subscription columns, so `revenue_truth.entitled_tier()` answers None) — no
pricing or tier code is involved. It is minted with
`is_sandbox_operator=False`, and checkout/billing are JWT-only surfaces an
`x-api-key` never authenticates against, so the fences need no new predicates.
