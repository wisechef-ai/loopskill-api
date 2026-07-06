"""Single source of truth for the application version.

Phase 0 of ``loopskill_activate_0701``: the version was previously duplicated
as a string literal in four modules (``main.py`` twice, ``health_routes.py``,
``core_routes.py``), which let the deployed instance report a stale number and
made "is the live box running the code we shipped?" unverifiable from
``/api/healthz``. Every deploy that changes behaviour MUST bump this constant
so the healthz probe can prove the cutover landed.

fix/skill-artifact-identity: bumped past live prod (0.9.4, verified via
GET /api/healthz) and current main (0.9.4) — this PR rebrands the /skill
install artifact, no schema change.

feat/unified-search: bumped past live prod (0.9.5, verified via GET
/api/healthz) and current main (0.9.5) — this PR adds the new anonymous
GET /api/search endpoint, no schema change.

feat/bundle-detail-artifact-parity: bumped past live prod (0.9.6, verified via
GET /api/healthz) and current main (0.9.6) — bundle detail now returns declared
personalities + composite_loops sections, no schema change.

feat/org-scoped-bundle-reads: bumped past live prod (0.9.7, verified via
GET /api/healthz) and current main (0.9.7) — org members get READ access to
org-scoped bundles (list/detail/manifest/sync/feedback-config), mirroring the
TEN org rule that fleets already had. Writes stay owner-only. No schema change.

feat/fleet-console-state: bumped past live prod (0.9.8, verified via
GET /api/healthz) and current main (0.9.8) — sync-report lockfile_state is now
PERSISTED (member_lockfile_snapshots, one upserted row per member) and served
via GET /fleets/{id}/members/{mid}/state + GET /fleets/{id}/inventory
(installed / drift / extras). Schema change: fc0706_lockfile_snap migration.

fix/lockfile-snap-uuid-types: bumped past live prod (0.9.9) and current main
(0.9.9) — fc0706b_snap_uuid corrects member_lockfile_snapshots id columns
VARCHAR(36)→UUID (Postgres 500'd on the first snapshot upsert; SQLite tests
could not catch the dialect mismatch).
"""

__version__ = "0.9.10"
