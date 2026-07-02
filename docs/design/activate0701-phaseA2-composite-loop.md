# Phase A2 — COMPOSITE LOOP + PERSONALITY DEPLOY — design contract (loopskill_activate_0701)

Author: Tori (opus-role design). Implementer: subagent (dispatch AFTER A1 merges). Reviewer: codex gpt-5.5.
Council conditions binding (docs/design/activate0701-phaseA1-council-report.md §6):
NEW surfaces only — separate table, separate routes, NEW MCP names. NEVER reuses /api/loops,
loopskill_search_loops, loopskill_get_loop, or portal /loops this sprint.

## The composite Loop (Kopadze 5-block model + state)
A deployable autonomous work unit: automation(heartbeat/cron) + skills + sub-agents(maker≠checker)
+ connectors + verifier(gate) + state_seed.

## Model
```python
class CompositeLoop(Base):
    __tablename__ = "composite_loops"
    id UUID pk; slug String(255) unique index; title String(512); description Text
    is_public Boolean default True; is_archived Boolean default False
    creator_id UUID FK creators nullable; org_id UUID FK orgs nullable
    tier String(32) nullable
    # ── composition (v2 amendments all present) ──
    schedule Text nullable=False               # cron expr or "30m" shorthand — the automation block
    skills JSON nullable=False default=list    # [{slug, pinned_version?}]
    connectors JSON default=list               # [{slug, pinned_semver?}]
    subagents_config JSON default=dict         # STRUCTURED: {maker: {model_tier, toolsets[]}, checker: {...}} — validated shape, not free text
    verifier_slug String(255) nullable=False   # FK-by-slug to the (renamed) Verifier registry
    state_seed JSON default=dict               # initial state document
    budget_usd Numeric(10,2) nullable          # Loop-LEVEL budget (distinct from verifier max_turns)
    prompt Text nullable=False                 # the loop's driving instruction
    residency String(32) nullable              # DERIVED server-side: most-restrictive of member artifacts (Phase E consumes)
    install_count/created_at/updated_at

class CompositeLoopVersion(Base):
    __tablename__ = "composite_loop_versions"
    id; composite_loop_id FK index; semver; manifest JSON (frozen full composition); changelog; created_at
    UniqueConstraint(composite_loop_id, semver)
```

## Validation (app/services/composite_loop_validation.py)
- verifier_slug must resolve to an existing public (or same-org) Verifier — the gate is REQUIRED.
- skills entries must resolve; connectors must resolve; subagents_config schema-validated
  (maker+checker keys, model_tier in {opus,sonnet,haiku,local,any}, toolsets list of strings).
- budget_usd > 0 when present. schedule parseable (croniter if dep exists, else regex for
  cron-5-field | '<N>m|h' shorthand).
- residency DERIVATION at publish: null unless any referenced connector carries residency_tag;
  then most-restrictive propagates (non-eu taints the loop non-eu). Store derived value; test it.
- provenance mint: deploying a composite loop to a member mints a provenance_id
  (reuse app/services/provenance.py seam) carried into LoopRun reports (Phase T field exists).

## Routes + MCP (NEW names only)
- /api/composite-loops: POST (auth), GET list (public, keyset), GET {slug} (public),
  POST {slug}/versions.
- Public prefix entry + write guards (mirror /api/loops guard tests).
- MCP: loopskill_publish_composite_loop, loopskill_get_composite_loop, loopskill_search_composite_loops.
- Bundles: BundleCompositeLoop join (pattern = BundleConnector from Phase B); reconcile diff gains
  composite_loops section; version publish bumps declaring bundles' generation (+ regression test).

## Personality-in-desired-state (lock #15, D11)
Probe first: Personality model exists (models.py:1389) + get/search MCP tools. Verify whether
bundles can declare personalities + whether reconcile carries them. Verified-absent path:
- BundlePersonality join + diff section + client apply: personality lands as a file in the
  profile's personalities dir (~/.hermes/personalities/<slug>.md or config-referenced path —
  inspect Hermes personality layout in ~/.hermes on THIS box; hermes-personality-system skill
  documents it). Apply is file-drop + optional config key — NO restart needed (unlike connectors).
- Client: extend reconcile_cli personality handling analogous to skills (atomic write + lockfile entry).

## Agent-side composite-loop apply (deploy-not-execute — lock #1-12)
Deploying a composite Loop lays down: (a) the loop manifest at
~/.hermes/loopskill/loops/<slug>/manifest.json, (b) a Hermes cron job spec the agent's fleet owner
loop can register (v1: manifest + documented `hermes cron add` line in the manifest — automated
cron registration is OWN/CUT-phase territory; do NOT auto-mutate cron state in A2), (c) ensures
member skills/connectors/personality deps are in the same bundle (server-side preflight warns on
missing deps at declare time).
atomic-habits + dreaming: author real manifests as fixtures + publish them to the live catalog
(parent session does the live publish; implementer ships the manifests under examples/loops/).

## Gates
- v2 gates: composite schema validated; instance_key semantics (member-side instance identity =
  member_id + loop slug — instance_key minted at deploy, stored in lockfile).
- Personality deploys to Tori via reconcile and lands in profile config (parent runs live).
- Council kill-tests: /api/loops payload UNCHANGED (regression re-run); no old-name reuse
  (grep test: composite modules contain zero references to loop_routes handlers).

## Tests (tests/test_activate0701_a2_composite_loop.py) — RED first
publish happy/validation failures (missing verifier, bad subagents shape, bad schedule)/
residency derivation matrix/bundle declare + diff + generation bump/personality diff + apply/
provenance minted on deploy/instance_key in lockfile/public browse + write guards/
old-surface regression (byte-identical /api/loops response vs A1 fixture).
