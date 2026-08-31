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

fleetos_1607 Phase E (0.9.28): BYO-repo registries — metadata-only = the
hyperscale gate. Two additive tables (artifact_origins, origin_drift_events) +
app/services/byo_origin.py. A private fleet brings its OWN GitHub repo; LoopSkill
stores the artifact ORIGIN (github:owner/repo@<sha>:<path>, SHA always — tags move,
force-push exists) + a content-hash LOCK, and NOTHING ELSE. Agents fetch the bytes
DIRECTLY from the user's repo with the user's token (secretRef) and verify the
fetched content's hash against the lock — refusing FAIL-CLOSED on mismatch and
recording an origin-drift event (force-push / tamper / wrong-SHA audit trail).
Server content_bytes_stored == 0 by design — storage stays flat per private fleet
(the hyperscale receipt, §0 #8). Public catalog unchanged (durable store).
10 RED-proofed tests (SHA-only pin, lock storage, hash-verify OK, force-push
fail-closed + drift event, failed-fetch fail-closed, metadata-only footprint).
Migration a520ed06c5d2 round-trips on SQLite + Postgres. Additive-only.

fleetos_1607 Phase D (0.9.29): fleet run registry — honest event semantics.
Upgrades the shipped loop_runs facts table (activate_0701, at-least-once emitter,
no dedup) into an HONEST registry. Five additive nullable columns (tick_id,
attempt, placement_epoch, member_seq, stale_epoch) + app/services/run_registry.py:
dedup on (loop, tick, attempt, epoch) so duplicate delivery cannot inflate a pass
rate; `unknown` is a first-class outcome distinct from fail (a killed / non-terminal
run is unknown, excluded from the pass numerator, visible in the total); a
stale-epoch run (epoch < the loop's current live placement epoch) is flagged and
excluded from pass numerators but counted in the health denominator. pass_rate =
passes / (total - unknown - stale), honest None when nothing counts. fleet_state +
trust_ledger_view expose the same numbers (trust-log.sh API parity). Existing prod
rows (NULL tick_id) are exempt from dedup by design. 6 RED-proofed tests. Migration
7c51d9bc2d36 additive (ADD COLUMN, all nullable) — safe on the populated prod table.

fleetos_1607 Phase T (0.9.30): the trojan skill — the fleet control-plane front
door. GET /fleet/skill serves a complete fleet-control-plane SKILL.md
(docs/fleet-skill/SKILL.md) as text/plain, no redirect (larrybrain pattern) —
any agent that reads markdown becomes a fleet CLIENT in one curl (enroll,
reconcile, report runs, harvest, and with an operator key, placements). Public
GET-only (added to APIKeyMiddleware.EXEMPT_PATHS); distinct from /skill (the
marketplace skill). Registered in main.py + tests/_app_factory._ROUTER_SPECS.
AGENTS.md refreshed with the fleetos control-plane framing + the dispatch_chain
convention. 4 RED-proofed tests (cold-agent keyless 200, alt paths, route in real
create_app, EXEMPT_PATHS pin). No schema, no migration.

fleetos_1607 Phase C (0.9.31): golden bundle composition + bootstrap planner.
app/services/golden_bundle.py composes ALL artifact types (loop manifests +
aggregated secret_refs + personalities/soul + host_profile ref) into one
declarative desired state (compose_golden_bundle), and plans a restore/kickstart
onto a target host (plan_bootstrap) with the host_profile validated FIRST — an
incompatible host yields ok=False with the NAMED unmet requirement, and missing
required secrets block with the named secret (a restore never silently proceeds
onto an incompatible host). triage_loops_for_bundle classifies loops portable vs
host-bound (the coverage audit IS the triage). The phoenix drill itself is
host-gated (needs a fresh VM) so it ships as a standing predicate +
scheduled/manual op: ~/.hermes/goals/checks/phoenix-drill-current.sh (drill <100d).
5 RED-proofed tests (compose gathers+dedups, bootstrap ok on compatible host,
fail-named on incompatible host, fail on missing secret, portable/host-bound
triage). No schema, no migration. Reuses Phase 0 validate_host_profile +
manifest_to_transport.

fleetos_1607 Phase I (0.9.32): the ingest surface — the missing write path.
Phases 0/A shipped LoopManifest + FleetMemberLiveness schema + read/compute/authz
but NO tool ever WROTE those rows, so the placement chain was inert by
construction (assign can't fire without a manifest; preflight can't pass without
a liveness row). app/mcp/tools/fleet_ingest.py adds loopskill_ping (member
advertises liveness + typed provides{}, upsert) and loopskill_declare_loop
(owner declares/updates a loop's desired-state manifest, upsert by
(owner-scope, loop_id) with version bump). Both owner/master-gated (bare member
key -> 403). Wired into the delegated dispatch chain. 9 RED-proofed tests incl.
the KEYSTONE test_ingest_makes_assign_succeed: declare_loop + ping => preflight
passes => assign creates a real placement at epoch 1 (the exact chain that was
inert). No schema, no migration.

0.9.33 - ClawHub origin_url is owner-scoped (issue #139). ClawHub skill pages
    live at /<ownerHandle>/skills/<slug>; the bare /skills/<slug> form we minted
    307-redirects to /skills/skills/<slug>, a soft-404 that still answers HTTP
    200 (SPA shell). Measured 69,150 of 90,605 federated rows (76.3%) advertising
    a dead deep link — and ClawHub is deep_link-only by policy, so the link IS
    the deliverable. New app/services/clawhub_url.py centralises URL building +
    a cached owner resolver; all four mint sites route through it. Ingest cannot
    resolve 69k owners inline (snapshot carries no handle) so it fails safe to
    the browse page. Verified against prod /api/healthz 0.9.32 before bumping.

0.9.34 - feat/loops-value-taglines: value_tagline + agent_instructions +
    deploy_hint on the /api/loops (Verifier) surface. The composite-loop
    surface shipped these serve-time copy fields (PRs #135, #136) but the 10
    starter loops on /api/loops never got them — every browse card was dead
    copy. Mirrors the composite pattern exactly (no DB column, no migration):
    per-slug bespoke taglines ≤12 words for all 10 starter loops + generic
    fallbacks for future user-published verifiers. Verified against prod
    /api/healthz 0.9.33 before bumping.

0.9.35 - fix(issue-157 Phase 1): removed "cookbook" from every MCP tool
    description and inputSchema property description an agent reads on
    every tool listing (26 violations across 4 registry modules — bundle
    install/list/sync/skillify, share tokens, fleet subscribe/sync/list,
    tailor attach/handoff, streaming install/pick/compose, feedback
    config). Pure copy change, zero behaviour change: the wire-contract
    field name `cookbook_id` and the live route
    `/api/cookbooks/{id}/install` are intentionally left alone (schema-
    breaking renames are Phase 3/4 per the issue's phasing, tracked
    separately). This is Phase 1 of 5 from issue #157 — the words agents
    and users actually read, for ~5% of the total work. Verified against
    prod /api/healthz 0.9.34 before bumping.

0.9.36 - fix(issue-219 item 1): LOOPSKILL_API_KEY dual-accept for the
    agent-side loop-apply tooling. tools/recipes_cli.py already had this
    (qa0208-w3); scripts/install-loop-apply.sh and
    meta-skills/recipes-auto-improve/incident_reporter.py still hard-read
    the legacy RECIPES_API_KEY name only, so a user who exported only
    LOOPSKILL_API_KEY (the branded name docs/MCP config use everywhere
    else) got a confusing "set RECIPES_API_KEY" error with a perfectly
    valid key. LOOPSKILL_API_KEY now wins when both are set; RECIPES_API_KEY
    keeps working unchanged as a fallback. Pure env-var resolution change,
    no schema, no migration. Verified against prod /api/healthz 0.9.35
    before bumping.

0.9.37 - agent/tori/first-impression-api: first-impression fixes on the
    cold-path a stranger actually hits (bundle discover -> install.sh).
    (1) requires_pro on GET /api/bundles/discover cards and GET
    /api/bundles/public/{slug} — computed from member skill tiers (reuses
    bundle_wellknown_routes._is_free / _is_redistributable_external
    verbatim, so discover/public-page/install.sh can never disagree on
    which member is free). (2) install.sh: when a bundle's
    installable-free count is 0, prints "This bundle requires Pro — 0 free
    skills will be installed. See https://app.loopskill.io/pricing" as the
    FIRST line (before any progress output) and exits 4 (distinct from the
    pre-existing usage=2 / missing-python3=3 codes) — closes a live defect
    where dev-agent-essentials/research-and-report (100% Pro) silently
    printed "installed 0 skill(s)" with no explanation. (3) /api/mcp/healthz:
    SERVER_NAME "recipes-mcp" -> "loopskill-mcp" (dead brand); SERVER_VERSION
    now reads app.version.__version__ directly instead of a frozen "0.1.0"
    literal, so the MCP identity handshake tracks real deploys. (4)
    /api/skills/external bare-call default (empty enabled_sources) was
    audited and left AS-IS — it is Adam's explicit isolation-namespace
    directive (see federation.py's module docstring, "off by default"),
    already has a pinned regression test
    (test_evergreen_f2_external_route.py::test_toggle_off_by_default_returns_no_external),
    and changing it would silently start querying live upstream federation
    adapters (rate-limited GitHub search, etc.) on every anonymous cold
    load with no user action — not a safe default flip. No schema, no
    migration. Verified against prod /api/healthz 0.9.36 before bumping.

0.9.38 - fix(issue-149, Option B, owner-approved 2026-08-19): the 3rd
    owner-facing bundle read path — GET /api/cookbooks/{id}/manifest —
    is now federated-aware, matching GET /api/cookbooks/{id} and POST
    .../install (both already fixed by sp2607fix-1/#150). Before this,
    a fleet owner syncing off the manifest silently lost every liked
    federated skill (BundleSkill.skill_id IS NULL rows) that detail/install
    already showed them — three endpoints, three different member counts
    for the same bundle. Federated entries carry federated_slug (no local
    Skill row exists, so `slug` is None) + federated/federated_source
    discriminators, merged into the same global (install_order, added_at,
    id) order the Composer contract requires. The 2 remaining public/
    anonymous surfaces (_public_cb_card, public_cookbook_page) and both
    .well-known routes stay explicitly LOCAL-ONLY pending §0b badging —
    documented inline at each call site per the issue's own Option B scope,
    not a regression. Pure additive response field (manifest YAML gains
    federated/federated_source/federated_slug keys on federated entries
    only); local-only bundle manifests are byte-identical (contract-pinned
    test). No schema change. Verified against prod /api/healthz 0.9.36
    before bumping.

0.9.39 - fix(issue-155): Skill.title no longer lands title-less. Extracted
    the backfill script's derivation logic into app/skill_title.py (shared,
    tested) and wired it into the publish path: a NEW skill whose manifest
    name equals its slug now gets a derived title instead of title==slug;
    a REPUBLISH of an existing skill no longer regresses a good editorial
    title back to a slug-shaped value just because that publish's manifest
    name happens to equal the slug (a live bug in the pre-fix re-sync
    block). Two RED-proofed regression tests. No schema, no migration.
    Verified against prod /api/healthz 0.9.36 before bumping.

0.9.40 - feat(identity): agent self-registration via Ed25519 proof-of-key.
    POST /api/agents/register lets an inbound agent that discovered LoopSkill
    machine-side enrol WITHOUT a human OAuth round-trip: it proves possession
    of an Ed25519 private key (signature over a canonical challenge + a
    single-use nonce) and receives a scoped, low-cap `rec_agent_` key. The
    .well-known agent/mcp descriptors are unauthenticated so the discovery
    step works before any credential exists. This is the only mechanism by
    which an external creator can EXIST under the zero-outreach constraint
    (creator-flywheel plan v2.1, Loop C) — every other enrolment path ends at
    a human clicking OAuth.

    Hardened over four adversarial review rounds (codex gpt-5.6-sol as the
    independent seat). Round 4 replaced the round-3 sliding-window bucket
    arithmetic — which sol proved bypassable across bucket boundaries — with
    lock-and-count: take a write lock on the per-scope gate row, THEN count
    the actual agent_identities rows in the trailing 24h under that lock, so
    the count-then-insert window is closed by Postgres until COMMIT and the
    alternate-boundary bypass is structurally impossible. Retry-After is the
    real seconds until the oldest in-window row exits, computed from the same
    clock as the refusal. Nonces are canonical-form-only (one spelling, one
    burn); pubkeys are canonicalised before identity comparison; agent-issued
    keys are fenced out of the human-facing surfaces.

    Migration agentreg0819_agent_identities is reversible. Known gap, stated
    rather than hidden: the concurrency proof is a 20-thread SQLite test plus
    a code-level argument for Postgres — no live PG concurrency harness runs
    in CI yet.

0.9.41 - fix(issue-157 Phase 1b): removed "cookbook" from every error-code
    string an agent or API consumer reads back on a 4xx — `detail=` on
    HTTPException routes AND the `{"error": ...}` dict shape used by
    promotion/reconcile routes and MCP tool error returns (bundle install,
    share tokens, fleet subscribe/sync, tailor handoff, streaming install,
    recipify, preflight). Renamed 37 occurrences across 20 modules:
    cookbook_not_found -> bundle_not_found, not_cookbook_owner ->
    not_bundle_owner, skill_not_in_cookbook -> skill_not_in_bundle,
    personality_not_in_cookbook -> personality_not_in_bundle,
    loop_not_in_cookbook -> loop_not_in_bundle, invalid_cookbook_id ->
    invalid_bundle_id, plus 3 free-text detail strings ("Token scope
    mismatch (wrong cookbook)" etc). Wire-contract field names
    (`cookbook_id` param, `/api/cookbooks/*` routes) are intentionally
    untouched — those are Phase 3/4 per the issue's phasing (breaking,
    needs a dual-emit window). Verified no portal src/ consumer keys off
    any of these string VALUES (grep clean) before renaming — copy-only,
    zero behaviour change. Verified against prod /api/healthz 0.9.36
    before bumping.

0.9.42 - gap/gap-aiplugin: closed the last walled AI-plugin discovery
    surface. GET /.well-known/ai-plugin.json — the 3rd standard AI-plugin
    discovery convention alongside agent.json/mcp.json — 401'd live in prod
    (verified 2026-08-20, `curl https://app.loopskill.io/.well-known/
    ai-plugin.json` -> `{"detail":"Invalid or missing x-api-key header"}`)
    because the route did not exist at all: any request to it hit
    APIKeyMiddleware's allowlist wall before routing could even resolve
    whether a handler existed. Added the route to the SAME module that
    already serves agent.json/mcp.json (app/agent_wellknown_routes.py,
    mirroring its _etagged_json/_CACHE_CONTROL conventions exactly) plus the
    exact path to app/middleware/_public_paths.py:EXEMPT_PATHS — that
    allowlist entry is the whole fix, same as the original agentreg_0819
    defect. The manifest's `api.url` deliberately points at
    /.well-known/mcp.json + /api/mcp/http (both genuinely exist and answer)
    rather than /openapi.json, which 404s live on this deployment by design
    (verified via curl: bare Caddy 404, no uvicorn header — the edge never
    proxies that path here) — the exact "documented but broken" defect
    class this route exists to kill. `auth` states the real self-registration
    flow (Ed25519 proof-of-key via POST /api/agents/register, no OAuth) and
    cross-links to /.well-known/agent.json for the full enrolment spec.
    8 RED-proofed tests in tests/test_gap_aiplugin_wellknown.py (confirmed
    to fail — 7 of 8 — with the EXEMPT_PATHS entry removed, matching the
    live 401 defect byte-for-byte before the fix): 200-anonymous,
    Cache-Control/ETag convention parity with agent.json/mcp.json,
    ai-plugin schema-shape, no /openapi.json reference, cross-link to
    agent.json, no-secrets-leak, EXEMPT_PATHS pinning, and a route-table
    introspection test that every URL the manifest advertises is a route
    that genuinely exists in app.routes (the no-404-links promise, pinned
    against reality rather than hardcoded). No schema, no migration.
    Verified against prod /api/healthz 0.9.41 before bumping.

0.9.43 - fix(money-path-3): first-touch UTM/ref attribution capture at
    signup. 2026-08-12 money-path audit found post→signup attribution
    entirely dark: the short-link redirector (app/utm_redirects.py) sets a
    ref cookie at click time and app/referral.py captures referral codes at
    signup, but nothing captured UTM/ref context AT signup — User.utm_ref
    was only ever written weeks later, at Stripe paid-conversion webhook
    time, and only if the cookie happened to survive that long. Adds
    User.signup_attribution (new nullable JSON column, additive migration
    bd8afe172c89 — deliberately NOT a repurposing of utm_ref, which has a
    different writer/lifecycle/validation shape) + app/services/
    signup_attribution.py:resolve_signup_attribution (validates + bounds
    utm_source/medium/campaign/content + ref, cookie-wins-over-query
    first-touch precedence, never raises). Wired into both OAuth callbacks
    (auth_routes.py) as a write-once, best-effort side effect — attribution
    capture can never block sign-in, including when all non-essential
    cookies are blocked or the attribution commit itself fails. No change
    to Stripe/webhook code, pricing, or the existing utm_ref column.
    Verified against prod /api/healthz 0.9.36 before bumping.

0.9.38 - fix(money-path-3 review): codex REQUEST_CHANGES on PR #250 — 7
    findings, all reproduced with evidence before fixing. WRITE-ONCE is now
    ATOMIC: the read-then-write `if user.signup_attribution` race (two
    concurrent callbacks for a brand-new user could both read NULL and the
    second write would silently clobber the true first touch) is replaced
    by a conditional `UPDATE ... WHERE signup_attribution IS NULL` checked
    via rowcount — portable across Postgres and SQLite, no dialect-specific
    locking. A commit failure inside the attribution write is now caught
    and immediately followed by db.rollback() in the SAME function, so a
    DB hiccup there can no longer poison the session and 500 the rest of
    the signup request (create_jwt reads user attributes right after) —
    reproduced with a genuine SQLAlchemy IntegrityError through the real
    OAuth callback request path, not a mock. The `recipes_utm_ctx` cookie
    (JSON UTM context) previously had NO producer anywhere in this repo —
    a callback-only capture with a documented-but-dead cookie path. Fixed
    the part that IS in this repo's power: /api/auth/{github,google}/login
    now accept optional utm_source/utm_medium/utm_campaign/utm_content
    query params and stamp them into the SAME short-lived cookie the
    callback already reads, mirroring exactly how ?ref= already survives
    the OAuth round-trip (WIS-660). The landing-page capture step itself is
    the PORTAL's responsibility (a different repo) — documented as a
    portal-side contract (forward its own page's UTM query params onto the
    "Sign in" link) rather than shipped as a silent dead path. The
    `creator:<handle>` ref-cookie trust bypass (a stale comment claimed
    pre-validation at cookie-set time, but the cookie is client-writable
    with no signature — a forger could mint `recipes_utm_ref=creator:
    anything` directly) is fixed: resolve_signup_attribution now takes an
    optional `db` and RE-VALIDATES creator: refs against the live
    Creator.handle table (same lookup _resolve_ref_value already uses);
    without a db handle the claim is unverifiable and is dropped, fail
    closed. `if user.signup_attribution` (falsy-overwrite: an empty dict
    `{}` — a valid "recorded, nothing to attribute" state — read as
    absent) is eliminated by construction: the new atomic UPDATE checks
    `IS NULL` at the SQL layer, where `{}` is NOT NULL and correctly blocks
    a second write. The `recipes_utm_ctx` cookie is now size-bounded
    (2048 bytes) BEFORE json.loads ever runs, closing a per-request
    unbounded-parse DoS lever on a fully client-controlled value. 15 new
    tests added (39 total in the money-path-3 suite): write-once race
    no-op, commit-failure recovery + signup-still-succeeds, existing-{}
    not overwritten, forged/unverifiable/real creator: ref, oversized ctx
    cookie discarded pre-parse, Google OAuth callback variant, and the
    utm_ctx cookie producer's full login->callback round-trip. No schema
    change. Verified against prod /api/healthz 0.9.37 before bumping.

0.9.44 - fix(issue-289): federation-registry-propose workflow now dedups
    against config/federation_sources.yaml BEFORE opening any GitHub issue.
    Issue #288 was a live instance of the defect this closes: the
    auto-proposal bot re-filed `[federation-registry] anthropics/skills`
    when github-anthropic (repo: anthropics/skills) has been a registered
    github_taps entry since decision #13 (2026-08-11) — each duplicate cost
    a full triage cycle (read config, cross-reference, comment, close).

    app/services/federation_sources_config.py:find_registered_github_repo
    does a case-insensitive lookup of the proposed repo_slug against every
    live github_taps row (reuses the existing github_tap_rows() SSOT reader,
    zero new state). Wired into loopskill_propose_registry (the ONE function
    both POST /api/federation/propose and the loopskill_propose_registry MCP
    tool call — app/mcp/tools/federation_propose.py) BEFORE the rate-limiter
    write and the DB insert, so an already-registered proposal costs the
    caller nothing (no rate-limit spend, no DB row, no GitHub issue) and is
    reported back honestly: `{"status": "already_registered",
    "existing_source_id": "github-anthropic", "review_channel_open": false}`.

    3 new tests (already-registered short-circuit, case-insensitive match,
    regression guard that a genuinely new repo still dispatches normally) —
    RED-proofed: both new-behavior tests fail on pre-fix code
    (`assert 'pending_review' == 'already_registered'`), pass after the fix.
    Breaker pass: empty/None input, 100k-char input, whitespace+case
    boundary match, near-miss no-false-positive (skills2/skill/xanthropics),
    and injection-shaped strings (path traversal, shell metacharacters) —
    all handled without raising or false-matching. No schema, no migration,
    no .github/workflows change (the dedup check lives in the shared Python
    choke point both proposal surfaces already funnel through — a smaller,
    more central fix than the issue's own suggested workflow-file edit).
    Verified against prod /api/healthz 0.9.43 before bumping.
"""

__version__ = "0.9.44"
