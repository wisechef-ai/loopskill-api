# CHANGELOG — recipes-api

All notable changes to recipes-api are documented here. Format loosely follows [Keep a Changelog](https://keepachangelog.com/) — semver per release.

## [Unreleased]

## [v0.5.0] — 2026-05-20 — recipes_2005 sprint

Release notes (creator-facing): see the `v0.5.0-creator-onboarding` blog post on recipes.wisechef.ai.

### Added — 9 new MCP tools (14 → 23)

- **`recipes_publish_request`** — guarded creator-publish path. Opens a labelled GitHub issue for human review; on `approved` label, a GitHub Action runs the multipart `_publish` server-side with an ephemeral ed25519 keypair. Zero client-side cryptographic juggling.
- **`recipes_share_create`**, **`recipes_share_list`**, **`recipes_share_revoke`**, **`recipes_share_rotate`** — cookbook share-token MCP wrappers around the existing `cookbook_share_tokens` table. Returns ready-to-paste Hermes YAML and Claude Desktop JSON config blocks.
- **`recipes_fleet_create`**, **`recipes_fleet_subscribe`**, **`recipes_fleet_sync`**, **`recipes_fleet_list`** — multi-agent fleet sync. Wires the orphan `Fleet` + `FleetSubscription` schema with an `x-fleet-key` middleware path (prefix `rec_fleet_*`).

### Added — infrastructure

- `app/_creator_helpers.py` — shared `_resolve_or_create_creator(ctx, db)` helper, used by both `publisher_routes.publish_skill` and `app/recipify.py:write_cookbook_skill`. Eliminates the silent `creator_id=NULL` drift on new MCP-published skills.
- `app/_config_block_formatter.py` — Hermes YAML + Claude Desktop JSON snippet generator for share-token responses.
- `app/services/fleet_sync.py` — fleet sync aggregation service used by `recipes_fleet_sync`.
- `app/middleware.py` — `rec_fleet_*` prefix path (additive, ordered after `cbt_*`).
- `app/authz.py` — `can_use_fleet(ctx, fleet)` predicate.
- `app/auth_ctx.py` — `scope='fleet'` + `fleet_id` field.
- `app/admin_routes.py` — `GET /api/admin/skill-publish-requests/{id}/tarball` (master-key only) for human review of pending publish requests.

### Added — workflows

- `.github/workflows/skill-publish-approver.yml` — triggers on `approved` label, runs multipart `_publish` server-side.
- `.github/workflows/feedback-dispatcher.yml` — extended with `skill-publish-request` event branch.

### Added — migrations

- `c3051b7d2005_add_skill_publish_requests.py` — `skill_publish_requests` table for tracking pending publish flows.
- `g1h2i3j4k5l6_tier_drift_sweep.py` — DB sweep `cook` → `pro`, `operator` → `pro_plus` on every live skill row.
- `6250d327ebf7_merge_c3051b7d2005_and_g1h2i3j4k5l6.py` — alembic merge revision.
- `a8b9c0d1e2f3_bootstrap_legacy_tables.py` — bootstraps `users`, `api_keys`, `creators`, `creator_payouts`, `referrals`, `skill_versions` (created out-of-band by `Base.metadata.create_all()` before alembic existed and never CREATEd by the chain). Idempotent — no-op on prod (tables already exist).
- `h2i3j4k5l6m7_tier_drift_sweep_archived.py` — completes the tier-slug migration by sweeping ARCHIVED rows the original Phase G sweep deliberately skipped. 15 prod rows (12 archived `cook` + 3 archived `operator`) renamed to canonical `pro`/`pro_plus`. Defensive guard asserts zero legacy slugs (`cook`/`operator`/`studio`) remain anywhere in the table — no orphans when the 30-day READ-alias window closes on 2026-06-10. Postgres regression test in `tests/migrations/test_chain_postgres.py`.

### Changed

- **Tier vocabulary renamed**: DB uses `{free, pro, pro_plus}` everywhere. Legacy `{cook, operator, studio}` accepted as 30-day READ aliases through 2026-06-10 (removal in `recipes_2006_legacy_cleanup`).
- Recipify default tier `'pro'` (was `'cook'`) — no more drift on new inserts.
- `TIER_RANK` collapses to `{free: 1, pro: 2, pro_plus: 3}` with legacy aliases as input shims.
- Skill body paywall: hydrates SKILL.md content for authenticated Pro/Pro+ users; free-tier skill bodies remain visible anonymously.
- pgvector migration `c5d6e7f8a902` now checks `pg_available_extensions` UP-FRONT instead of wrapping `CREATE EXTENSION` in try/except — the old pattern left the alembic transaction in an aborted state on vanilla `postgres:16` containers (no pgvector), breaking the migration chain at the next ALTER. Clean dialect-aware branch.

### Fixed

- `?tier=pro` filter on `/api/skills/search` returned 0 results despite DB having 52 pro rows — three call sites kept the pre-rename alias map (`pro → cook`). Inverted to `cook → pro` (legacy READ alias).
- Recall service default `tier_filter` no longer silently excludes paid skills when caller passes no filter.
- `recipes_recipify` now respects `tier`, `is_public`, and creator-id kwargs (was silently dropping all three for MCP callers, requiring SQL backfill for every creator).
- Migration test suite no longer silently passes when the alembic chain is broken — see `tests/migrations/test_chain_postgres.py` which exercises the real Postgres dialect.

### Infrastructure

- 8 GitHub Actions workflows migrated `ubuntu-latest` → self-hosted `wisechef-runner` (unblocking deploys during the org's GH-Actions billing block).
- `.github/actionlint.yaml` registers the `wisechef-runner` label so pre-commit's actionlint hook stops rejecting it.

### Tests

- 75+ new tests across phases B–I (recipify trio, publish_request, share MCP, fleet MCP, tier rename, CLI portability, portal bugs, carousel + docs).
- NEW `tests/migrations/test_chain_postgres.py` — exercises the full alembic chain against real Postgres (the prod dialect). Three checks: chain runs cleanly from baseline → head; bootstrap migration creates expected tables; model columns all present.
- NEW `scripts/test-migrations-against-postgres.sh` — one-shot local Postgres migration runner (`pgvector/pgvector:pg16` or `IMAGE=postgres:16` for CI parity).

[Unreleased]: https://github.com/wisechef-ai/recipes-api/compare/v0.5.0...HEAD
[v0.5.0]: https://github.com/wisechef-ai/recipes-api/releases/tag/v0.5.0
