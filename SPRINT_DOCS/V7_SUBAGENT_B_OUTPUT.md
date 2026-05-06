# v7 Phase B — Subagent B Output

## Summary

Cookbook CRUD endpoints, tier gates, and manifest export. All 8 endpoints in
spec landed; 17 tests added (target was 12+). Baseline test suite stable
(701 passing = 684 baseline + 17 new; 10 failed / 15 errored unchanged).

## Files

- **app/cookbook_routes.py** (new) — 8 endpoints + tier gate (`require_cookbook_tier`)
- **app/main.py** (2-line patch) — import + `include_router(cookbook_router)`
- **tests/test_cookbook_routes.py** (new) — 17 tests across 6 classes

## Endpoints

| Method | Path                                      | Notes                                              |
|--------|-------------------------------------------|----------------------------------------------------|
| POST   | `/api/cookbooks`                          | Free → 401, Cook → 1-max (403 on 2nd), Op/Studio ∞ |
| GET    | `/api/cookbooks`                          | List mine via `cookbook_owner == user.id`          |
| GET    | `/api/cookbooks/{id}`                     | Detail + skills (incl. disabled, with source)      |
| POST   | `/api/cookbooks/{id}/skills`              | Validates slug; reactivates disabled rows          |
| DELETE | `/api/cookbooks/{id}/skills/{slug}`       | Soft-delete: `source='disabled'`, row preserved    |
| POST   | `/api/cookbooks/{id}/install`             | Idempotent; skips disabled; resolves SkillVersion  |
| GET    | `/api/cookbooks/{id}/manifest`            | YAML response (`application/x-yaml`)               |
| GET    | `/api/cookbooks/{id}/sync?since=<iso>`    | Partitions added/updated/removed by `source`       |

## Tier gate semantics

- Free / no-tier / inactive sub → **401** (`{needs_tier: "cook"}`)
- Cook tier with 1 cookbook already → **403** (`{reason: "cook_tier_limit"}`)
- Cook (0 cookbooks), Operator, Studio → **201**
- Master key bypasses tier check; cannot create user-owned cookbooks (400)

## Test coverage

- `TestTierGates` (5): free→401, no-tier→401, cook→201, cook 2nd→403, operator unlimited
- `TestListDetail` (3): list filters by owner, detail includes skills, cross-user→404
- `TestAddRemoveSkill` (3): add succeeds, unknown slug→404, delete soft-deletes
- `TestManifest` (1): YAML round-trip including `pinned_version`
- `TestInstall` (2): idempotency (two POSTs equal payload), disabled skipped
- `TestSync` (3): since-filter splits t1/t2/t3, source partitioning, invalid since→422

## Test command

```bash
env -u PYTHONPATH WR_DATABASE_URL=sqlite:///:memory: DATABASE_URL=sqlite:///:memory: \
  .venv/bin/python -m pytest tests/test_cookbook_routes.py -q --tb=short
# -> 17 passed in ~1.5s
```

## Caveats / phase boundaries

- **`install` URL shape**: returns `https://<origin>/api/skills/{skill_id}/versions/{version_id}/tarball`
  — that route does NOT exist yet. Phase C is expected to either add it or
  swap to a signed-URL pattern (mirror of `/api/forks/_download`). The current
  payload structure (`slug`, `version`, `tarball_url`, `checksum_sha256`,
  `source`) is the stable contract; only the URL host/path will firm up later.
- **`sync` is poll-based** today (returns full snapshot filtered by `added_at`).
  Phase D will layer SSE streaming on top — the response shape
  (`{added, removed, updated}` partitioned by `source`) is the contract Phase D
  consumes.
- **`since` parameter** uses `CookbookSkill.added_at` only. There is no
  separate `updated_at`/`disabled_at` column on `CookbookSkill`, so a
  later-disabled skill still uses its original `added_at`. This is acceptable
  given Phase B's audit-trail-via-row-retention design; Phase D may add a
  dedicated event log table if real-time delta granularity becomes a concern.
- **Cook tier 1-max** is enforced via simple `COUNT(*)`. Race condition under
  concurrent POSTs is theoretically possible but a non-issue at Cook tier
  scale. No DB unique constraint added (would have required a migration; spec
  said avoid).

## Phase C/D/E hooks

Downstream phases import `app.cookbook_routes.router` and the helpers
`require_cookbook_tier` / `_resolve_owned_cookbook`. The `Cookbook` and
`CookbookSkill` models were not modified.
