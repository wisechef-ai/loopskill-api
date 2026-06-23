---
name: super-memory
description: >
  One-command installer for the full agent memory stack: cognee 1.0.x knowledge
  graph (pghybrid), LiteLLM proxy with rotation, cognee-api watchdog cron,
  nightly ingest pipeline, CouchDB 3 single-node document store, and a
  filesystem markdown vault wired to Obsidian. Install order is encoded in the
  11 numbered scripts; the top-level install.sh --detect-first orchestrator
  runs only what is missing. Use when provisioning a fresh Linux box or
  replicating a production-grade agent memory stack anywhere.
tier: free
os_supported: [linux, macos]
tags: [cognee, litellm, couchdb, postgres, pgvector, obsidian, watchdog, memory, vault, nightly-ingest]
triggers:
  - install cognee memory stack
  - set up super-memory
  - provision memory for agent
  - replicate a full agent memory stack
  - cognee + couchdb + obsidian
related_skills:
  - cognee-kuzu-to-pghybrid-migration
  - cognee-litellm-proxy-rotation
  - cognee-api-watchdog
  - cognee-nightly-ingest-optimization
  - cognee-retrieval-architecture
  - cognee-llm-provider-swap
  - cognee-minor-version-upgrade
  - cognee-agent-coordination
  - vault-context-loader
system_requirements:
  cpu_cores_min: 2
  ram_gb_min: 8
  disk_gb_min: 20
  ports_required: [5432, 4000, 8100, 5984]
  os_supported: [ubuntu-22.04, ubuntu-24.04, debian-12]
---

# super-memory

Your AI agent gets the same memory stack described here -- cognee knowledge graph +
auto-watchdog + nightly ingest + Obsidian-ready markdown notes -- installed in
10 minutes. Free recipe; bring your own host. Cancel anytime, take your data with you.


## Architecture

```
                     super-memory stack
  ┌──────────────────────────────────────────────────────┐
  │  ~/super-memory-vault/   (markdown files, SCHEMA.md) │
  │           │                                           │
  │           ▼ nightly-ingest cron                      │
  │  cognee-api :8100   (cognee 1.0.x, pghybrid mode)    │
  │     │           │                                     │
  │     │           └─► Postgres 16 + pgvector :5432      │
  │     ▼                                                 │
  │  LiteLLM proxy :4000  (Ollama→OR-free→gpt-4o-mini)   │
  │                                                       │
  │  CouchDB 3 :5984   (document sync / Obsidian LiveSync)│
  │                                                       │
  │  cognee-api-watchdog cron (hourly)                    │
  │  nightly-ingest cron     (02:00 daily)                │
  └──────────────────────────────────────────────────────┘
```

## Decision Tree

Use this before running any script:

```
Q1: Is this a brand-new box (nothing installed)?
  YES → run: bash install.sh
  NO  → continue to Q2

Q2: Does `bash scripts/90-verify.sh --detect` return state:ok?
  YES → stack is healthy, no action needed
  NO  → continue to Q3

Q3: Which component is partial/none?
  Postgres partial  → bash scripts/10-postgres-up.sh
  Cognee partial    → bash scripts/20-cognee-up.sh
  LiteLLM partial   → bash scripts/30-litellm-proxy.sh
  Watchdog missing  → bash scripts/40-watchdog-cron.sh
  Nightly missing   → bash scripts/50-nightly-ingest.sh
  CouchDB partial   → bash scripts/60-couchdb-up.sh
  Vault missing     → bash scripts/70-vault-bootstrap.sh
  Sync missing      → bash scripts/80-cognee-couchdb-sync.sh
  Obsidian missing  → bash scripts/85-obsidian-connect.sh

Q4: After targeted fix, still failing?
  → Check Troubleshooting section below
  → Run: bash scripts/00-preflight.sh (re-checks deps)
```

See references/decision-tree.md for the full graph-style version.


## Value Proposition

Your AI agent gets the same memory stack described here -- cognee knowledge graph +
auto-watchdog + nightly ingest + Obsidian-ready markdown notes -- installed in
10 minutes. Free recipe; bring your own host. Cancel anytime, take your data with you.


## Install Order

Scripts are numbered for a reason -- run them in sequence on a fresh box,
or let install.sh orchestrate based on --detect state:

```
00-preflight.sh         verify OS, ports, python, docker, disk
10-postgres-up.sh       postgres 16 + pgvector via docker
20-cognee-up.sh         cognee 1.0.x venv + systemd unit + .env
30-litellm-proxy.sh     litellm proxy venv + systemd unit + config
40-watchdog-cron.sh     crontab entry for cognee-api-watchdog.py
50-nightly-ingest.sh    crontab + launch-from-cron.sh for nightly batch
60-couchdb-up.sh        CouchDB 3 docker single-node + admin user
70-vault-bootstrap.sh   ~/super-memory-vault/ + SCHEMA.md scaffold
80-cognee-couchdb-sync.sh  sync connector (cognee recall → CouchDB docs)
85-obsidian-connect.sh  CouchDB LiveSync plugin config helpers
90-verify.sh            end-to-end health check -- all green = done
```

Top-level orchestrator: install.sh
Removal: uninstall.sh


## Pitfalls

### 1. cognee health.py Cypher try/except patch
Cognee 1.0.x health.py runs a Cypher query against the graph backend. With
pghybrid mode, the Cypher driver is absent and the unpatched code raises an
uncaught exception, returning HTTP 500 instead of 200. The fix: wrap the Cypher
check in try/except and fall through to the SQL ping. Script 20-cognee-up.sh
applies this content-anchor patch automatically.
Source: cognee-kuzu-to-pghybrid-migration, cognee-v1-api-migration

### 2. recall.py _safe_kwargs allowlist
cognee recall() uses _safe_kwargs to filter parameters before passing to the
backend. If you add custom retrieval parameters, they are silently dropped
unless added to the allowlist in cognee/api/v1/recall/recall.py. Symptoms:
custom node_name / search_type filters have no effect. Patch the allowlist or
use the REST API directly.
Source: cognee-retrieval-architecture

### 3. ALTER ROLE idle_in_transaction_session_timeout
A fresh Postgres install has all timeouts at 0 (unbounded). Cognee's async code
can hold transactions open across LLM calls (30-120s). Without a role-level
timeout, leaked idle-in-transaction backends pile up, exhaust the connection
pool, and cause /health 500s while the process appears alive. Script
10-postgres-up.sh applies: idle_in_transaction_session_timeout=60000,
statement_timeout=120000, idle_session_timeout=600000 for the cognee role.
Source: postgres-leaked-tx-watchdog

### 4. launch-from-cron.sh 600s mid-flight kill
Running the nightly ingest wrapper directly from a cron LLM agent kills the
process at ~600s (Hermes agent timeout) mid-flight. The correct invocation is
launch-from-cron.sh which uses setsid+nohup to fully detach before returning
in ~2s. Script 50-nightly-ingest.sh installs this wrapper. Never call
cognee-nightly-ingest.sh directly from a cron context.
Source: cognee-nightly-ingest-optimization

### 5. Content-anchor patches survive cognee minor upgrades
Scripts in this bundle use Python content-anchor matching (search for a known
code string, replace it) rather than line-number sed patches. This is critical
because cognee minor upgrades (1.0.3 → 1.0.5 → 1.0.x) shift line numbers.
A content-anchor patch keyed on a function signature survives any minor
version bump that doesn't rename the function. Always prefer content-anchor
style when patching installed Python packages.
Source: cognee-minor-version-upgrade

### 6. CouchDB 5984 port collision with ROS robot stacks
CouchDB listens on TCP 5984 by default. ROS (Robot Operating System) also uses
5984 for its parameter server on some distros. If this box runs a ROS stack,
change COUCHDB_PORT in the .env before running 60-couchdb-up.sh. Script
60-couchdb-up.sh checks for 5984 occupancy during --detect and reports a
conflict in the evidence JSON.
Source: couchdb-vault-design.md (references/), honcho-self-hosted

### 7. Obsidian LiveSync only works while app is open
The CouchDB LiveSync Obsidian plugin syncs vault files to CouchDB in real time,
but only while the Obsidian desktop app is running. It is NOT a background
daemon. For agent-side writes (e.g. nightly ingest writing new notes to the
vault), the sync runs on the next Obsidian open. Do not design workflows that
depend on sub-minute CouchDB propagation from agent writes unless you have a
separate file watcher pushing changes.
Source: obsidian-as-gui-addon.md (references/)

### 8. presidio dependency adds ~600MB
cognee 1.0.x optionally pulls in Microsoft presidio (PII detection). If you
install cognee[all] instead of the base package, pip will download ~600MB of
spaCy models + presidio. This skill installs cognee base + only the required
extras (httpx, pgvector, asyncpg). Use 'pip install cognee[base]' or the
explicit extras list in 20-cognee-up.sh.
Source: cognee-v1-api-migration


## Troubleshooting

### postgres fails to start
- Check port 5432 is free: ss -tlnp | grep 5432
- Check docker is running: systemctl --user is-active docker (or sudo systemctl)
- Check disk space: df -h /var/lib/docker

### cognee-api.service fails
- Check .env exists: ls ~/super-memory/cognee/.env
- Check postgres is up: docker ps | grep sm-postgres
- Check alembic migrations: cd ~/super-memory/cognee && .venv/bin/alembic current
- Tail logs: journalctl --user -u cognee-api -n 50

### LiteLLM proxy 4000 not responding
- Check service: systemctl --user is-active cognee-litellm-proxy
- Tail logs: tail -f ~/super-memory/litellm/logs/litellm-proxy.log
- Verify config syntax: python3 -c "import yaml; yaml.safe_load(open('~/super-memory/litellm/config.yaml'))"

### CouchDB 5984 returns 401
- Admin credentials are in ~/.hermes/super-memory.env
- Test: curl -u admin:PASSWORD http://127.0.0.1:5984/_up
- Container logs: docker logs sm-couchdb

### 90-verify.sh reports partial
- Run individual detect: bash scripts/XX-scriptname.sh --detect
- Each --detect returns JSON {state: ok|partial|none, evidence: {...}}
- evidence.reason explains what was found/missing

### Nightly ingest not running
- Check crontab: crontab -l | grep super-memory
- Check last run log: ls -lt ~/super-memory/logs/nightly-ingest-*.log | head -3
- Check launch wrapper: bash scripts/50-nightly-ingest.sh --detect


## Uninstall

```bash
bash uninstall.sh
```

Uninstall stops and removes all docker containers (postgres, couchdb), removes
systemd units, removes crontab entries (between the >>> super-memory <<< markers),
and optionally deletes the ~/super-memory-vault/ directory (prompts before delete).
Data in Postgres is lost when the container is removed unless you export first.
