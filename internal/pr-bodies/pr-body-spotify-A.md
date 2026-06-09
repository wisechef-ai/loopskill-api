# feat(spotify_0608-A): Cookbook FULL-absorbs Bucket (D1) — survivor primitive

**Plan:** `projects/recipes/2026-06-08-spotify_0608-execution-plan.md` · Phase A (CLEAN_TO_BUILD @ R4)

Cookbook becomes the survivor primitive (D1). This is the lossless absorption of the
retired `Bucket`/`BucketSkill` pair — the schema foundation Phases B–H depend on.

## What changed

### Model layer (`app/models.py`)
- **`Cookbook`** gains Bucket's presentation + white-label columns: `slug` (nullable, unique),
  `visibility`, `is_white_label`, `custom_domain`, `pin_mode`, `theme_json`.
- **New `CookbookDeployment` table** — the R3 data-model contract's lossless replacement for
  `BucketSkill`: `id PK, cookbook_id FK, skill_id NULLABLE, fork_id NULLABLE, version_pin,
  install_order` + the same `skill_id XOR fork_id` CHECK constraint. This is the *deployment*
  layer (ordered apply, forks, version-pin).
- **`CookbookSkill` is UNTOUCHED** — keeps its NOT-NULL `(cookbook_id, skill_id)` PK and every
  inner-join. Two tables, two concerns, zero join breakage.
- `Bucket` + `BucketSkill` models retired (replaced with a documentation block).

### Ported, not deleted (D1 boil-the-ocean)
| Retired | Re-homed to |
|---|---|
| `app/buckets_routes.py` | `app/cookbook_deployment_routes.py` (mounted `/api/cookbook-deploy`) |
| `app/bucket_loader.py` | `app/cookbook_loader.py` |
| `app/bucket_preflight.py` | `app/cookbook_preflight.py` |
| `app/middleware/bucket_routing.py` (`BucketHostMiddleware`) | `app/middleware/cookbook_routing.py` (`CookbookHostMiddleware`) |

White-label `custom_domain` host routing survives as a Pro+ "host your cookbook on your own
domain" capability. New prefix `/api/cookbook-deploy` is disjoint from the existing
`/api/cookbooks` CRUD — zero path collision.

### SSOT cleanup
- `access_routes`/`schemas`: canonical `cookbook_deploy_eligible`; `bucket_eligible` kept as a
  **non-breaking deprecated alias** (same value) for one release.
- JWT path allowlist `/api/buckets/` → `/api/cookbook-deploy/`.
- Dogfood data `internal/buckets/` → `internal/cookbooks/`.

### Migration `spotify_0608_a_cb_absorbs_bkt`
1. Adds the six columns to `cookbooks`.
2. Creates `cookbook_deployments`.
3. Migrates `buckets` → `cookbooks` 1:1 (reuses bucket UUID as cookbook id so the deployment
   FK remap is a straight copy).
4. Migrates `bucket_skills` → `cookbook_deployments` 1:1.
5. Drops `bucket_skills` then `buckets`.

Data-copy steps are Postgres-only (no-op on empty CI tables). **Fully reversible** —
downgrade recreates both tables and copies rows back losslessly.

> **Note for Phase B:** the `slug`+`visibility` columns are added HERE (Phase A) because the
> Bucket→Cookbook data migration physically requires them to exist before copying bucket slugs.
> Phase B consumes them for discovery and adds only its own net-new `is_test` integrity column —
> **B must NOT re-migrate slug/visibility.**

## Verification (real Postgres, not just CI sqlite)
- ✅ Migration upgrade: bucket→cookbook + bucket_skill→deployment 1:1, ordering preserved
  (10, 90), skill/fork split intact, white-label fields copied, old tables dropped, XOR enforced,
  native cookbooks default `private` and stay untouched.
- ✅ Migration downgrade round-trip: lossless (buckets + bucket_skills restored, orders intact,
  cookbook columns dropped).
- ✅ Revision id ≤32 chars (alembic_version is VARCHAR(32) — a 38-char id was caught failing the
  prod version stamp and shortened).
- ✅ Full suite green; CI lint parity: ruff, ruff-format, bandit, mypy all pass.
- ✅ Fixed a **pre-existing latent test bug** found via the cold worktree:
  `test_free_tier_allowlist.py` used the real file-engine without creating its schema, so it
  errored on any cold checkout (gitignored `test_dev.db`). Now self-bootstraps idempotently.

## Acceptance gate (plan §2 Phase A)
- [x] All Bucket capabilities reachable on Cookbook incl. ordered apply + forks + white-label
- [x] 0 orphaned Bucket refs (only historical doc-comments remain)
- [x] `CookbookSkill` joins unchanged
- [x] Full suite green
- [ ] Live-verify on prod (post-merge: `alembic upgrade head` + free→Pro ≤2-click gate)
