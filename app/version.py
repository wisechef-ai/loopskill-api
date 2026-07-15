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

feat/artifact-kind-phase1: bumped past live prod (0.9.10) and current main
(0.9.10) — adds `kind` discriminator + `loop_spec` JSON to skills table
(am0706_skill_kind migration). Foundation for merging CompositeLoop into Skill.
All existing rows default to kind='skill'; loop_spec is NULL.

fix/keyset-cursor-compound: bumped past current main (0.9.11) — fixes flaky
test_list_members_keyset_pagination. The keyset cursor predicate was `id >
cursor_id` alone, which breaks when created_at differs between rows (a member
with a later created_at but lex-smaller id gets skipped on page 2). Now uses
the standard compound row-value predicate (created_at, id) > (cursor_ca,
cursor_id). No schema change.

fix/issue-52: bumped past current main (0.9.12) — deflakes
test_cook_rate_limit_429 (UUID-decoded-as-float race in the shared in-memory
SQLite test session, via expire_on_commit=False in test db fixture). Test-only
change; bump keeps the healthz cutover-proof invariant intact.
fix/issue-63-rename-sweep: bumped past main (0.9.13) — docs/comments/brand
rename of stale "WiseRecipes"/"recipes" references to "LoopSkill". Live env
var names (WR_*, RECIPES_*) are unchanged for prod compatibility — marked with
TODO(rename) comments. Closes #63.

spotify_1507 Ph0 (0.9.19): bare GET /api/health is now a public, DB-independent
liveness status (was 401 — a cold-path trust leak); no_external_promo linter
allowlist widened with legitimate API-doc/source domains (arxiv, tavily, tenor,
stripe, comfyui, modal, etc.) so real orphan-tarball skills stop false-blocking.
"""

__version__ = "0.9.20"
