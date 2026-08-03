# TERMINOLOGY.md — LoopSkill canonical vocabulary (Phase 0 spec freeze)

> **Frozen 2026-06-22 (loopskill_0622).** This is the single source of truth for
> the LoopSkill rename. Every later phase and the `terminology-lint` CI gate
> greps against this file. Do not edit without a corresponding plan revision.

## Brand

| Old (kitchen) | New | Scope |
|---|---|---|
| Recipes (product) | **LoopSkill** | brand, domain `loopskill.io`, repos, packages |
| WiseChef (as the Recipes brand) | **LoopSkill** | brand strings ONLY |
| `recipes.wisechef.ai` | `loopskill.io` | canonical host; old host 301-redirects through parallel run |

## Catalog artifact types (the four)

| Type | What it is | Status |
|---|---|---|
| **skill** | a single capability (SKILL.md + scripts/refs) — the `Skill` model | EXISTS, v1 |
| **bundle** | a curated set of skills, deploy+sync to a fleet (was "cookbook") | EXISTS as `cookbooks`, v1 |
| **personality** | a deployable persona / SOUL (system prompt + config) | NEW, **v1 (pulled in 2026-06-22)** |
| **loop** | a shareable, safety-bounded autonomous agentic loop | NEW, **v1 (pulled in 2026-06-22)** |

> 2026-06-22 decision (Adam): personality + loop are **pulled into v1**, not
> fast-follow. A 100k-star OSS repo must ship its runnable hero artifacts at launch.

## Load-bearing primitive map

| Today | New | Where it lives |
|---|---|---|
| `cookbooks` (table) | `bundles` | DB, migrations |
| `cookbook_skills` | `bundle_skills` | DB, migrations |
| `cookbook_share_tokens` | `bundle_share_tokens` | DB, migrations |
| `cookbook_deployments` | `bundle_deployments` | DB, migrations |
| `cookbook_id` (FK) | `bundle_id` | DB, models, schemas |
| `/api/cookbooks` | `/api/bundles` | route prefix; old path = 301 alias |
| `/api/cookbook-deploy` | `/api/bundle-deploy` | route prefix; old path = 301 alias |
| `cbt_` (share-token prefix) | `bdl_` | middleware, models, salt-parity tests |
| salt `recipes-skill-install` | `loopskill-install` | install_routes + cookbook_routes + reconcile_fetch + regression suite |
| pkg `wisechef-recipes` | `loopskill` | npm / pypi |
| `recipes_*` MCP tools | neutral verbs (install/search/publish/deploy/sync/…) | 24 tool modules |

## Bundle visibility and what `bundle_limit` actually meters

`Bundle.visibility` has **three** values, not two:

| Value | Meaning | Metered against `bundle_limit`? |
|---|---|---|
| `private` | visible only to the owner. The default for every new bundle. | **yes** |
| `team` | shared with the owner's org/team, not published | **yes** |
| `public` | published to the community; discoverable, installable by anyone | **no — unlimited on every tier, including Free** |

`bundle_limit` in `config/tiers.yaml` (Free 2 · Pro 50 · Pro+ 200) is a
**private-bundle cap**, never a total-bundle cap. Public bundles are free and
unlimited on every tier: free community curation is a growth lever, so
publishing must never cost the curator a slot. Flipping a bundle
private→public frees its slot immediately, in the same request cycle.

`team` is metered because it is not community curation — and because metering
only `private` would be a one-click cap bypass (flip everything to `team` and
own unlimited quasi-private bundles).

**One implementation:** `app/services/bundle_quota.py`
(`count_metered_bundles` / `quota_status`). Every enforcer (REST
`POST /api/cookbooks`, MCP `loopskill_compose_bundle_from_links`) and every
display surface (`/api/billing/me`, `/api/auth/me`) reads it, so the number a
user is shown is the number they are held to. Do not write a second
owner-bundle count query.

Wire keys: `cookbook_limit` and the 403 detail `{"reason": "pro_tier_limit",
"max_cookbooks": N}` are **legacy external contract** — kept verbatim
(dual-accept). The visibility-qualified names `max_private_bundles` /
`private_bundles_used` are additive alongside them.

## Tier ladder — 3 public, 1 data-layer-only (autopilot_0308 M2, D-003)

| DB slug    | Public tier? | Notes |
|------------|--------------|-------|
| `free`     | yes          | |
| `pro`      | yes          | $9.95/mo, unchanged (D-004) |
| Enterprise | yes          | contact form, no price object, no `db_slug` — a sales conversation (hub D-005), not a meter |
| `pro_plus` | **no**       | data-layer only; see `docs/taxonomy.md` for the full rationale |

`pro_plus` is off the public ladder but is **not deleted** — it stays a
live `db_slug` for the D-010 migration window and for Enterprise contracts
(assigned manually, never via self-serve checkout). Full detail, including
why dropping it would be a data-loss migration, lives in
`docs/taxonomy.md`. This entry exists so a future rename/cleanup pass hits
this file's grep before touching that enum value.

## Dead legacy to DROP (not rename)

| Item | Action |
|---|---|
| `recipes` table (legacy, unused) | drop in Phase 3 migration |
| `/api/recipes/{slug}` compat route (`recipe_routes.py`) | drop in Phase 4 (keep 301 alias to `/api/skills/{slug}` through parallel run) |
| `api_library` table + `/api/api-library/{slug}` | audit in Phase 3; drop if dead |

## The fused term — PROTECTED, do NOT rename

| String | Protected sense | Why |
|---|---|---|
| **Chef** | the sister AGENT in the Wise-Agents fleet | brand rename must NOT touch the fleet agent |
| `chef` in agent-sync / fleet / cron / SOUL contexts | the AGENT | allowlisted in lint gate |

## Lint gate — banned nouns in NEW code/routes/UI

Banned (case-insensitive) in `app/`, `src/`, route strings, UI copy:
`cookbook`, `recipe`, and `chef` **as a brand**.

Allowlist (lint gate MUST NOT flag these):
1. Historical alembic migration **filenames** (`alembic/versions/*cookbook*.py` keep their names — renaming a migration file breaks the revision chain).
2. The **Chef AGENT** sense: any line also containing `agent`, `fleet`, `sister`, `SOUL`, `agent-sync`, `Wise-Agents`, or in `crons/`/agent-coordination contexts.
3. 301-compat alias definitions (explicitly tagged `# compat-alias` so the gate knows it's intentional).
4. This file (`TERMINOLOGY.md`), `ARCHITECTURE.md`, `CHANGELOG.md`, and `docs/migration/*` (they document the old terms by necessity).
5. Test files asserting compat behavior (tagged `# compat-test`).
