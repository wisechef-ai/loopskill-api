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

atomic-habits/2026-07-13-rank8-catalog-hygiene: bumped past main (0.9.23) —
adds a STARTER_LOOPS SSOT entry + LOOP_TAGS_BY_SLUG discovery tags for
repo-steward-loop, which was published straight against the live DB without
ever entering the seed pipeline (install_count=0, latest_version=null, zero
discovery tags). Re-running seed_starter_catalog.py now produces a v1.0.0
LoopVersion manifest for it carrying category + tags, matching the other 9
starter loops. Data-only, no schema change.

fleetos_1607 Phase 0 (0.9.25): the declarative fleet-artifact primitives that
turn LoopSkill from a marketplace into the control plane for AI agent fleets.
Three additive tables (loop_manifests, scripts_packs, host_profiles) + a pure
services module (app/services/fleet_artifacts.py): canonical loop-manifest
serialization with byte-identical round-trip, a scripts-pack secret-scan gate
that REUSES the shipped security_scan.scan_tarball (planted key => refused,
RED-proofed), and host-profile compatibility validation (typed requires{} vs
os/runtimes/packages). The soul artifact was deleted by the 5-step pass — the
existing Personality model already is the deployable-SOUL primitive. Migration
547f9f97e64d is portable (plain CREATE TABLE, no PL/pgSQL) and round-trips on
SQLite + Postgres. Additive-only, no data migration.

fleetos_1607 Phase A (0.9.26): placements — the spine. Three additive tables
(loop_placements, placement_confirmations, fleet_member_liveness) + the
epoch-CAS placement service (app/services/placement.py): every transition is a
compare-and-swap on a monotonic placement_epoch, so two concurrent writers
cannot both win. Cooperative move = drain (epoch++) -> old-member confirm (deduped
on member_seq) -> activate-new (epoch++); force_move retires the old placement,
flags forced=True, and surfaces per-safety-class duplicate-risk text (no
exactly-once claim, honest-guarantee doctrine). A Postgres partial unique index
enforces the single-live-placement invariant at the DB layer. Manager surface
(assign/evacuate/placements/force_move MCP tools) is gated by the new
authz.can_manage_fleet capability — a bare fleet-member key gets 403, an
operator/owner/master key gets through. Stale-member alert
(app/services/stale_member_alert.py) replaces the deleted Phase F failover.
13 RED-proofed tests. Additive-only, no data migration.

fleetos_1607 Phase B (0.9.27): harvest — reverse GitOps via the SHIPPED feedback
rail. An agent submits its live-state manifest; the server diffs it against the
golden bundle (new-local / modified-local / missing-local) and routes the drift
back as a proposal through the EXISTING loopclose_3005 Phase J rail (per-bundle
feedback_repo + Fernet PAT vault + dispatch_issue) — ZERO new tables, ZERO new
auth model (§0 #13). Every harvested loop is secret-scanned + path-escape-scanned
BEFORE it can become a proposal (a poisoned member is BLOCKED, never proposed);
reports are HMAC-signed by the member key (lock #13). No feedback_repo configured
=> in-app feed fallback. The MCP _dispatch god node was refactored: the delegated
dispatch chain (fleet-write / placement / harvest) moved to app/mcp/dispatch_chain.py
to keep server.py under the 600-line gate. 11 RED-proofed tests (diff, poison
block, signature, routing, non-owner 403, end-to-end). Additive-only, no migration.
"""

__version__ = "0.9.27"
