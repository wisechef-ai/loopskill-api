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
