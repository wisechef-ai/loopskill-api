# Self-Host LoopSkill — Fleet Control Plane

LoopSkill is the GitOps fleet control plane for AI agents. Declare desired state
server-side; agents pull and converge. This guide walks a cold clone to a working
fleet loop using only the docs below.

## Quick Start (cold clone → fleet → reconcile → outcome records)

### 1. Clone + boot

```bash
git clone https://github.com/wisechef-ai/loopskill-api.git
cd loopskill-api
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Set required env vars (see wiserecipes-api.env.example for the full list — the filename is legacy, env var names still use WR_*/RECIPES_* prefix for prod compatibility)
# TODO(rename): env var still uses legacy name for prod compatibility — see issue #63
#
# EVERY server setting is read with the WR_ prefix (app/config.py sets
# env_prefix="WR_", and alembic/env.py reads WR_DATABASE_URL). Exporting a bare
# name such as DATABASE_URL does NOT fail loudly — it falls through to the
# default in alembic.ini and migrates a database you never named.
export WR_API_KEY="your-master-key"
export WR_JWT_SECRET="your-jwt-secret"
export WR_SIGNING_SECRET="your-signing-secret"
export WR_HEARTBEAT_PEPPER="your-pepper"
export WR_OAUTH_REDIRECT_BASE="https://your-domain.example.com"
export WR_DATABASE_URL="postgresql://user@localhost/loopskill"

# GitHub OAuth. Step 2 mints your owner key from a signed-in session, and the
# only way to sign in is GitHub — so without these two you get to step 2 and
# stop. Create them at https://github.com/settings/developers → "New OAuth App";
# set the callback URL to $WR_OAUTH_REDIRECT_BASE/api/auth/github/callback.
export WR_GITHUB_CLIENT_ID="Ov23li..."
export WR_GITHUB_CLIENT_SECRET="..."

# OPTIONAL but high-leverage: GitHub code-search federation.
#
# The `github-oss` federated source discovers public skills across GitHub by
# code-searching `filename:SKILL.md` (>5,000,000 matches as of 2026-08). GitHub's
# code-search API rejects anonymous requests with 401, so WITHOUT a token this
# source degrades to a GRACEFUL EMPTY: it logs one info line and indexes zero
# rows, with `last_error = NULL` in `federation_index_cache`. It looks healthy
# and returns nothing — verified on prod 2026-08-10, where github-oss had been
# indexing 0 for want of this single variable.
#
# A fine-grained token with PUBLIC READ-ONLY scope is sufficient; no repo write,
# no org access. Either name is accepted.
export GITHUB_TOKEN="github_pat_..."   # or GH_TOKEN

# Verify it took effect after boot:
#   select source, indexed_count, last_error from federation_index_cache
#     where source = 'github-oss';
# indexed_count = 0 AND last_error IS NULL means the token is missing or unset
# in the SERVER's environment (systemd EnvironmentFile, not just your shell).
#
# Note: `well-known` legitimately indexes 0 — it is discovery-by-URL and has no
# central catalog to crawl. See STRUCTURALLY_EMPTY_SOURCES in
# app/services/federation_live.py. Do not chase it as a bug.

# Run migrations
alembic upgrade head

# Boot
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. Get an owner key

Three different keys appear in this guide. Mixing them up is the most common
way step 3 fails with a 403, so name them once:

| Key | Where it comes from | What it may do |
|---|---|---|
| **master** — `$WR_API_KEY` | the env var you exported in step 1 | everything server-side, but it is **not** a user, so it cannot own a bundle |
| **owner** — `rec_live_…` | minted below, for your signed-in account | create bundles + fleets, declare + place loops |
| **member** — `rec_live_…` | returned once by step 5 | reconcile, read its own loop assignments, post telemetry |

Sign in first — the key mint is a session route, not an API-key route:

```bash
# Browser: https://your-host/api/auth/github/login  (needs WR_OAUTH_REDIRECT_BASE
# from step 1 to point at this host). Then, with that session cookie:
curl -X POST https://your-host/api/api-keys \
  -b "$COOKIE_JAR" \
  -H "Content-Type: application/json" \
  -d '{"label": "self-host owner key"}'
# → {"key": "rec_live_…"}  — returned ONCE. This is rec_live_OWNER_KEY below.
```

Free and Pro accounts may hold **one** active key at a time; a second mint
returns 403 `key_cap_exceeded`. Revoke with `DELETE /api/api-keys/{key_id}`.

### 3. Create a bundle and put a skill in it

A bundle is the unit of desired state — the fleet subscribes to *it*, not to
loose skills. Nothing later in this guide works without its UUID.

```bash
# Bundles are born PRIVATE. The master key cannot do this: a bundle needs an
# owner, so use the owner key from step 2.
curl -X POST https://your-host/api/bundles \
  -H "x-api-key: rec_live_OWNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-stack", "description": "what my agents run"}'
# → {"id": "<bundle-uuid>", ...}  — this is <bundle-uuid> everywhere below.

# Add a skill by slug (any slug from GET /api/skills/search?q=…)
curl -X POST https://your-host/api/bundles/<bundle-uuid>/skills \
  -H "x-api-key: rec_live_OWNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"slug": "hub-search-claude-code"}'

# Sanity check — this is what a member will converge to:
curl -s https://your-host/api/bundles/<bundle-uuid>/manifest \
  -H "x-api-key: rec_live_OWNER_KEY"
```

### 4. Create a fleet and subscribe it to the bundle

```bash
curl -X POST https://your-host/api/fleets \
  -H "x-api-key: rec_live_OWNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-fleet"}'
# → returns fleet_id and fleet_key ONCE — save both.

# Subscribe the fleet to the bundle on a channel
curl -X POST https://your-host/api/fleets/<fleet_id>/subscribe \
  -H "x-api-key: rec_live_OWNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"cookbook_id": "<bundle-uuid>", "channel": "canary"}'
```

### 5. Enroll an agent as a fleet member

```bash
curl -X POST https://your-host/api/fleets/<fleet_id>/members \
  -H "x-api-key: rec_live_OWNER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"host": "my-agent-host", "profile": "default", "skills_dir": "~/.loopskill/skills"}'
# Returns the member's dedicated API key ONCE — save it.
```

### 6. Set up the agent-side sync loop

On the agent host, install the sync script:

```bash
# The reconcile CLI is in the repo:
pip install -e /path/to/loopskill-api

# Set the member API key
export RECIPES_API_KEY="rec_live_MEMBER_KEY"  # TODO(rename): env var still uses legacy name for prod compatibility

# Run one reconcile cycle:
python -m app.reconcile_cli \
  --cookbook <bundle-uuid> \
  --api https://your-host \
  --skills-dir ~/.loopskill/skills \
  --lockfile ~/.loopskill/state/lock.json

# For connectors (optional):
python -m app.reconcile_cli \
  --cookbook <bundle-uuid> \
  --api https://your-host \
  --skills-dir ~/.loopskill/skills \
  --lockfile ~/.loopskill/state/lock.json \
  --connector-config ~/.config/hermes/config.yaml \
  --gateway-restart-cmd "systemctl --user restart hermes-gateway"
```

### 7. Set up a 30-minute cron

```bash
# crontab -e
# TODO(rename): RECIPES_API_KEY env var still uses legacy name for prod compatibility
*/30 * * * * RECIPES_API_KEY=rec_live_MEMBER_KEY /path/to/venv/bin/python -m app.reconcile_cli \
  --cookbook <bundle-uuid> \
  --api https://your-host \
  --skills-dir ~/.loopskill/skills \
  --lockfile ~/.loopskill/state/lock.json \
  --api-key rec_live_MEMBER_KEY \
  >> ~/.loopskill/sync.log 2>&1
```

### 8. Report outcomes (Phase T — batched sync-report)

The sync-report collector batches loop runs, skill errors, and cron health
into ONE POST per cycle:

```bash
# Install the collector (shipped in the repo):
install -m 0755 scripts/loopskill-collect-reports.py ~/.hermes/scripts/
LOOPSKILL_MEMBER_KEY=rec_live_MEMBER_KEY python3 ~/.hermes/scripts/loopskill-collect-reports.py

# Emit a loop-run outcome:
scripts/loopskill-emit-run.sh <loop_slug> <outcome> [accepted] [cost_usd] [duration_s] [detail]
```

`scripts/install-loop-apply.sh` (see **Running loops**, below) places both of
these for you.

### 9. Verify the loop is working

```bash
# Check fleet health:
curl https://your-host/api/fleets \
  -H "x-api-key: rec_live_OWNER_KEY"

# Check voice inbox:
curl https://your-host/api/fleets/<fleet_id>/voice-inbox \
  -H "x-api-key: rec_live_OWNER_KEY"

# Check healthz:
curl https://your-host/api/healthz
```

## Running loops

Everything above converges **skills** onto a member. This section adds
**loops** — the scheduled agent jobs that actually do work. It is the same GitOps shape: you
declare desired state server-side, the agent pulls and converges.

The whole chain, and the file that owns each hop:

| # | Hop | Owner |
|---|-----|-------|
| 1 | Declare the loop's manifest | MCP `loopskill_declare_loop` |
| 2 | Place it on a member | MCP `loopskill_assign` |
| 3 | The member reads its assignments | `GET /api/my/loop-assignments` |
| 4 | Materialize a local cron | `python -m app.loop_apply_cli` |
| 5 | The cron fires | your host scheduler |
| 6 | The loop reports its outcome | `scripts/loopskill-emit-run.sh` |
| 7 | The outcome is batched + POSTed | `scripts/loopskill-collect-reports.py` |
| 8 | Telemetry lands | `POST /api/sync-report` |

> **Host support, plainly:** hops 4–5 are wired for **Hermes** hosts only.
> `app/loop_apply.py` writes the Hermes scheduler's `~/.hermes/cron/jobs.json`
> document, and nothing else speaks that format yet. The skill path above works
> on any host; the loop path **does not yet** on Codex, Claude or OpenCode, and
> `install-loop-apply.sh` will refuse them rather than install a cron that can
> never converge. Hops 1–3 and 6–8 are host-agnostic.

### 10. Install the loop-apply + collector crons

One command, on the agent host, from a clone of this repo. Idempotent — running
it twice replaces its own cron block rather than adding a second one.

```bash
# The MEMBER key from step 5 — loop assignments are a member surface;
# a fleet/bundle key gets a 403 here.
export RECIPES_API_KEY="rec_live_MEMBER_KEY"

bash scripts/install-loop-apply.sh --api https://your-host
# → installs ~/.hermes/scripts/loopskill-emit-run.sh
# → installs ~/.hermes/scripts/loopskill-collect-reports.py
# → writes the key to ~/.hermes/loopskill/member.key (0600)
# → wires two */30 crons: app.loop_apply_cli, then the collector
```

Preview without writing anything:

```bash
bash scripts/install-loop-apply.sh --api https://your-host --dry-run
```

### 11. Declare a loop and place it on the member

Both are MCP tools on the fleet-manager surface, called with the **owner** key
(the member key cannot declare or place — that is deliberate). Point any MCP
client at `https://your-host/api/mcp/http/` with an `x-api-key` header:

```json
{
  "mcpServers": {
    "loopskill": {
      "type": "http",
      "url": "https://your-host/api/mcp/http/",
      "headers": { "x-api-key": "rec_live_OWNER_KEY" }
    }
  }
}
```

Then call two tools:

```jsonc
// 1. loopskill_declare_loop — the desired state, upserted by loop_id
{
  "fleet_id": "<fleet-uuid>",
  "loop_id": "my-health-check",
  "schedule": "*/30 * * * *",      // 5-field cron, or "30m" / "every 2h"
  "prompt": "Run the health check, then report the outcome by running:\n  bash ~/.hermes/scripts/loopskill-emit-run.sh my-health-check success\n",
  "safety_class": "idempotent",     // idempotent | best-effort | manual-only
  "state_class": "stateless",
  "deliver": "local"
}

// 2. loopskill_assign — bind it to the member that will run it
{
  "fleet_id": "<fleet-uuid>",
  "loop_key": "my-health-check",
  "member_id": "<member-uuid>",
  "op_id": "assign-1"              // idempotency key; a retry is not a second placement
}
```

`loopskill_assign` runs a capability preflight first and refuses with the named
missing requirements rather than placing onto a member that cannot run the loop.
A member that has never pinged fails with `liveness:member-never-pinged` — call
`loopskill_ping` for it once.

**The loop's prompt is what emits its telemetry.** Nothing else does. If the
prompt never calls `loopskill-emit-run.sh`, the loop will run forever and report
nothing — this is the single most common way a loop looks healthy and is not
measured. Put the emit line in the prompt when you declare the loop.

### 12. Watch it converge

```bash
# What the server thinks this member should be running:
curl -s https://your-host/api/my/loop-assignments \
  -H "x-api-key: rec_live_MEMBER_KEY"
# → {"count": 1, "assignments": [{"loop_key": "my-health-check",
#      "status": "active", "epoch": 1, "manifest": {...}}]}

# Materialize it now instead of waiting for the cron:
RECIPES_API_KEY=rec_live_MEMBER_KEY python -m app.loop_apply_cli \
  --api https://your-host --dry-run     # diff only, writes nothing
RECIPES_API_KEY=rec_live_MEMBER_KEY python -m app.loop_apply_cli \
  --api https://your-host
# → {"status": "applied", "created": ["my-health-check"], ...}
```

Managed jobs are namespaced `loopskill/<loop_id>` and tagged
`loopskill-managed`. The manifest is the source of truth: loop-apply creates,
updates and **removes** jobs in that namespace as your assignments change, and
never touches a cron outside it. Each job records the placement epoch it was
built from, so a stale read cannot overwrite a newer apply.

```bash
# The cron loop-apply just wrote:
python3 - <<'EOF'
import json, pathlib
jobs = json.loads((pathlib.Path.home() / ".hermes/cron/jobs.json").read_text())["jobs"]
for j in jobs:
    if j["name"].startswith("loopskill/"):
        print(j["name"], j["schedule_display"], j["last_status"], j["last_run_at"])
EOF
```

### 13. Watch a run land

After the cron fires, the loop's own emit call leaves a record in the spool; the
collector drains it on its next cycle.

```bash
ls ~/.hermes/loopskill/outbox/*.json          # spooled, not yet sent
python3 ~/.hermes/scripts/loopskill-collect-reports.py
# → sync-report: 1 loop_runs, cron errors=0
ls ~/.hermes/loopskill/outbox/.sent/           # sent, kept for audit
```

The emitter never fails its caller and never touches the network: bad arguments,
a full disk or an unreachable server are a warning on stderr and `exit 0`. A
loop must not die because its telemetry could not be written. The collector is
the only component that talks to `/api/sync-report`, and it exits 0 on network
failure and retries next cycle.

### 14. Move or retire a loop

```jsonc
// evacuate — remove the placement; the next loop-apply deletes the local cron
{ "fleet_id": "<fleet-uuid>", "loop_key": "my-health-check", "op_id": "evac-1" }

// loopskill_placements — read the current state of every loop in the fleet
{ "fleet_id": "<fleet-uuid>", "include_removed": true }
```

Undeploy is not "stop scheduling it server-side" — the next `loop_apply_cli`
cycle removes the `loopskill/<loop_id>` cron from the host. You buy the loop and
its maintenance.

## Architecture

```
Fleet Owner (you)                LoopSkill Server                Agent (member)
┌─────────────┐                 ┌─────────────────┐            ┌──────────────┐
│ Create fleet│ ──── POST ────► │ Fleet + Bundle  │            │              │
│ Enroll agent│ ──── POST ────► │ FleetMember     │            │              │
│ Publish skill│ ── POST ─────► │ Skill + Version │            │              │
│ Declare state│ ── POST ─────► │ BundleSkill     │            │              │
└─────────────┘                 │                 │            │              │
                                │  Reconcile API  │ ◄── POST ──││ Pull (30m)  │
                                │  Sync-report    │ ◄── POST ──││ Report     │
                                │  Heartbeat      │ ◄── POST ──││ Liveness   │
                                └─────────────────┘            └──────────────┘
```

## API Surface

| Endpoint | Method | Auth | Description |
|----------|--------|------|-------------|
| `/api/healthz` | GET | public | Version + DB status |
| `/api/fleets` | GET/POST | user | List/create fleets |
| `/api/fleets/{id}/members` | GET/POST | fleet owner | List/enroll members |
| `/api/fleets/{id}/voice-inbox` | GET | fleet owner | Aggregated voice stream |
| `/api/bundles/{id}/reconcile` | POST | member key | Conditional reconcile poll |
| `/api/bundles/{id}/reconcile-report` | POST | member key | Report apply outcome |
| `/api/sync-report` | POST | member key | Batched loop_runs + cron_health |
| `/api/connectors` | GET/POST | public/user | Browse/publish connectors |
| `/api/composite-loops` | GET/POST | public/user | Browse/publish composite loops |
| `/api/v1/heartbeat` | POST | anonymous | Liveness ping |
| `/api/mcp/http/` | POST | api key | MCP StreamableHTTP server |
