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
export WR_API_KEY="your-master-key"
export WR_JWT_SECRET="your-jwt-secret"
export WR_SIGNING_SECRET="your-signing-secret"
export WR_HEARTBEAT_PEPPER="your-pepper"
export WR_OAUTH_REDIRECT_BASE="https://your-domain.example.com"
export DATABASE_URL="postgresql://user@localhost/loopskill"

# Run migrations
alembic upgrade head

# Boot
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. Create an account + fleet

```bash
# Create a user (or sign in via GitHub OAuth at /api/auth/github/login)
# Then create a fleet via MCP or HTTP:
curl -X POST https://your-host/api/fleets \
  -H "x-api-key: rec_live_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"name": "my-fleet"}'
# Returns fleet_key ONCE — save it.

# Subscribe the fleet to a bundle (cookbook) on a channel
curl -X POST https://your-host/api/fleets/{fleet_id}/subscribe \
  -H "x-api-key: rec_live_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"cookbook_id": "<bundle-uuid>", "channel": "canary"}'
```

### 3. Enroll an agent as a fleet member

```bash
curl -X POST https://your-host/api/fleets/{fleet_id}/members \
  -H "x-api-key: rec_live_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"host": "my-agent-host", "profile": "default", "skills_dir": "~/.loopskill/skills"}'
# Returns the member's dedicated API key ONCE — save it.
```

### 4. Set up the agent-side sync loop

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

### 5. Set up a 30-minute cron

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

### 6. Report outcomes (Phase T — batched sync-report)

The sync-report collector batches loop runs, skill errors, and cron health
into ONE POST per cycle:

```bash
# Install the collector (copy from scripts/ or write your own):
python ~/.loopskill/scripts/collect-reports.py

# Emit a loop-run outcome:
./loopskill-emit-run.sh <loop_slug> <outcome> [accepted] [cost_usd] [duration_s] [detail]
```

### 7. Verify the loop is working

```bash
# Check fleet health:
curl https://your-host/api/fleets \
  -H "x-api-key: rec_live_YOUR_KEY"

# Check voice inbox:
curl https://your-host/api/fleets/{fleet_id}/voice-inbox \
  -H "x-api-key: rec_live_YOUR_KEY"

# Check healthz:
curl https://your-host/api/healthz
```

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
