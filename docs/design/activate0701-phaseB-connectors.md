# Phase B — CONNECTOR ARTIFACT + GUARDED APPLY — design contract (loopskill_activate_0701)

Author: Tori (opus-role design). Implementer: subagent. Reviewer: codex gpt-5.5.
Branch: loopskill_activate_0701/phaseB (worktree /home/adam/repos/wt-activate0701-phaseB, off main).
Locks: D8 (gateway restart allowed, guarded), lock #15 (Connector = deployable artifact class),
§0.5 secret discipline (${VAR} refs only, grep-proves 0 literal keys).

## What a Connector IS
A named MCP-server config fragment (the thing that goes under `mcp_servers:`/`tools:` in a Hermes
config.yaml — command/args/env/url) published as a versioned artifact and deployable to fleet
members via reconcile. Server stores the TEMPLATE with `${VAR}` env refs — the agent-side apply
resolves vars from the AGENT's environment. Literal secrets never transit the server.

## Server side (loopskill-api)

### Models
```python
class Connector(Base):
    __tablename__ = "connectors"
    id UUID pk; slug String(255) unique index; title String(512); description Text
    connector_type String(32)      # "stdio" | "http" | "sse"
    is_public Boolean default True; is_archived Boolean default False
    creator_id UUID FK creators.id nullable; org_id UUID FK orgs.id nullable
    residency_tag String(32) nullable   # "eu" | "non-eu" | null (Phase E consumes; TAG NOW)
    install_count Integer default 0
    created_at/updated_at DateTime(tz)

class ConnectorVersion(Base):
    __tablename__ = "connector_versions"
    id UUID pk; connector_id UUID FK connectors.id index; semver String(32)
    config_template JSON nullable=False   # the mcp-server block, ${VAR} refs
    required_env JSON default list        # ["ZAI_API_KEY"] — vars agent must have
    changelog Text nullable
    created_at DateTime(tz)
    UniqueConstraint(connector_id, semver)
```

### Publish-time validation (app/services/connector_validation.py)
- config_template must parse; connector_type-specific required fields (stdio: command; http/sse: url).
- SECRET LINT (hard reject): any value matching literal-secret patterns (sk-, rec_live_, Bearer
  <token>, 40+ char base64ish strings, IPs outside allowlist, /home/<user> paths). Only ${VAR}
  refs allowed for sensitive slots. Reuse discipline-lint needle logic where it exists (grep
  app/ for the skill-publish lint — security_scan.py / skill_quality_gate.py — and share helpers).
- required_env entries must each appear as ${VAR} in the template (consistency).

### Routes (app/connector_routes.py) + MCP tool
- POST /api/connectors {slug,title,description,connector_type,residency_tag} (auth: user/master) 201
- POST /api/connectors/{slug}/versions {semver, config_template, required_env, changelog} 201; 409 dup semver
- GET /api/connectors (public browse, keyset-paginated) + GET /api/connectors/{slug} (public)
- MCP tool loopskill_connector_publish (one call = create-if-missing + mint version) registered in
  app/mcp/ following the loopskill_* canonical pattern (_registry_loopskill.py + tools/ module).

### Desired-state integration
Bundles gain connector declarations. Follow the EXISTING BundleSkill pattern:
```python
class BundleConnector(Base):
    __tablename__ = "bundle_connectors"
    bundle_id UUID FK pk; connector_id UUID FK pk
    pinned_semver String(32) nullable   # null = track channel latest
    added_at DateTime(tz)
```
- POST /api/bundles/{id}/connectors {slug, pinned_semver?} / DELETE .../connectors/{slug}
- Reconcile diff (app/services/reconcile.py) gains a `connectors` section:
  {add|update|remove: [{slug, semver, config_template, required_env}]}. Client reports local
  connector state in the reconcile POST body (new optional `local_connectors` list — additive,
  backward compatible: absent field = no connector reconciliation).
- Publishing a new ConnectorVersion bumps declaring bundles' generation
  (bump_declaring_bundles in services/reconcile.py — EXTEND it; Phase 0 bug 4 class:
  every mutation that should invalidate the 304 token MUST, and MUST be tested).

## Client side (app/reconcile_client.py + reconcile_cli.py — the D8 apply path, HIGHEST CARE)

New module app/connector_apply.py (keep reconcile_client under 600 lines):
```
class ConnectorApplier:
    apply(connector_diff, config_yaml_path, gateway_restart_cmd, health_probe) -> ConnectorApplyResult
```
Sequence per D8, all-or-nothing:
1. SNAPSHOT: copy config.yaml -> config.yaml.lsk-backup-<ts> (keep last 3).
2. RESOLVE: for each add/update, check required_env vars exist in agent environment
   (os.environ) — missing var = REFUSE that connector pre-apply (structured error, no partial write),
   report `env_missing:<VAR>` in outcome. ${VAR} refs are written VERBATIM into config.yaml
   (Hermes resolves at load) — the check only proves resolvability.
3. PATCH: ruamel/yaml round-trip edit of the mcp-servers section ONLY (comment-preserving if
   ruamel available; else yaml.safe_load + targeted dict edit + safe_dump of managed block —
   decide by what requirements.txt already carries; do NOT add heavy deps casually).
   Managed-block discipline: connectors land under a clearly-marked `# --- loopskill-managed ---`
   region; never touch keys outside it.
4. RESTART: run gateway_restart_cmd (configurable; Tori: `systemctl --user restart hermes-gateway`).
5. HEALTH PROBE: configurable probe. Default = gateway process alive after N seconds; for http
   connectors optionally a live JSON-RPC initialize against the server. Probe timeout 30s.
6. ON FAIL: restore snapshot, restart again, re-probe. Report `rolled_back` w/ failure_reason.
   If restore-restart ALSO fails: report `rollback_failed` (CRITICAL) — never leave silently broken.
7. stage_only mode: per-fleet flag (Fleet.stage_only_connectors Boolean default False server-side,
   carried in the reconcile diff): write to config.yaml.lsk-staged instead, no restart, outcome
   `staged`.

CLI: reconcile_cli gains connector handling in the same run (after skills apply): apply connectors,
include outcomes in the status JSON so loopskill-sync.sh reports them via reconcile-report
(outcome vocabulary reuses success|reconcile_failed|rolled_back + staged).

## Gates (plan §2 Phase B)
1. zai_websearch connector published (template with ${ZAI_API_KEY}, residency_tag non-eu),
   declared in tori-core, deployed to Tori via a real reconcile cycle → Tori's config.yaml gains
   the managed block → gateway restart → live JSON-RPC initialize 200 serverInfo. (Parent session
   runs this live gate — implementer proves it in tests + a scripted dry-run.)
2. KILL-TEST: broken connector (bad command) deploy → health probe fails → auto-rollback restores
   snapshot → gateway survives → outcome rolled_back reported.
3. grep-proof: zero literal keys in any artifact (test asserts the secret lint rejects a literal-key
   template; test asserts published zai template contains ${ZAI_API_KEY} verbatim).

## Tests (tests/test_activate0701_connectors.py — RED first)
Server: publish happy/dup-409/secret-lint-reject/required-env-consistency; bundle declare;
reconcile diff carries connectors section; generation bump on new version (304 invalidation —
Phase 0 bug-4 regression class); public browse pagination; anonymous write 401.
Client (tmpdir fixtures, fake restart cmd = script flipping a marker file):
apply happy path writes managed block + preserves unrelated yaml keys; env_missing refusal (no
write); health-fail rollback restores byte-identical config; rollback_failed path; stage_only
writes staged file, no restart; snapshot rotation keeps 3.
Version: bump app/version.py minor (check current value first — parallel branches; note collision).
