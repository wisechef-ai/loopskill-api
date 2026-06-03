---
name: recipes-cookbook-reconcile
description: >
  Keep an AI agent's skills EVERGREEN. Install this once and your cookbook's
  skills stay current automatically — with atomic apply + auto-rollback so a
  bad skill version can NEVER leave your agent broken. This is the thin
  reconcile client for Recipes (recipes.wisechef.ai): it rides your host's
  existing scheduler (Hermes cron / Codex / Claude), pulls only the skills that
  changed (content-addressed delta, CDN-fronted), and atomically swaps them into
  your skills dir. On any health regression it auto-reverts to the last-known-good
  snapshot. Invoke when asked to "keep my skills up to date", "stop skill rot",
  "set up cookbook auto-sync", or "install the Recipes reconcile daemon". Pull-only
  (uses your own API key, no inbound auth); 304-cheap polling; self-updating (ships
  as a skill in the cookbook it manages, so it updates itself).
tier: free
category: maintenance
license: Apache-2.0
tags: [reconcile, sync, maintenance, evergreen, cookbook, gitops, auto-update, rollback]
related_skills: [super-memory]
os_supported: [linux, macos]
unhappy_paths:
  - condition: "RECIPES_API_KEY is missing or invalid when recipes-reconcile runs"
    recovery: "The client exits cleanly with code 2 and a one-line message (no traceback). Set RECIPES_API_KEY from recipes.wisechef.ai → settings → API keys (a free-tier key works for free skills) and re-run. A wrong key returns {\"status\": \"http_error\", \"code\": 403} and exit 3 — generate a fresh key."
  - condition: "A pulled skill version is broken (empty/corrupt SKILL.md, fails the health check)"
    recovery: "AUTOMATIC — the atomic client auto-reverts to the last-known-good snapshot, leaves recipes-lock.json untouched, and exits non-zero. Your agent keeps the working version; nothing to do. The bad version is also blocked from promoting past the canary channel."
  - condition: "Network is unreachable or recipes.wisechef.ai is down at reconcile time"
    recovery: "The client exits 3 with {\"status\": \"network_error\"} and makes no changes; the next scheduled cron tick retries. Reconcile is idempotent, so a missed tick simply catches up on the following run."
  - condition: "No agent host detected by install.sh (none of ~/.hermes ~/.codex ~/.claude ~/.opencode skills dirs exist)"
    recovery: "Pass --host explicitly (e.g. --host hermes) and ensure that host's skills directory exists, or create it first. install.sh refuses to guess a skills dir it can't find rather than writing to the wrong place."
  - condition: "Killed mid-apply (machine reboot, SIGKILL during a swap)"
    recovery: "AUTOMATIC — the lockfile read is resume-safe (a half-written lockfile reads as empty), and the next run re-snapshots and reconciles from a consistent state. The live skills dir is never left in a partially-swapped state."
---

# recipes-cookbook-reconcile — your skills never rot

The Recipes thin reconcile client. Install once; your cookbook's skills stay
evergreen on your host's own schedule, with **atomic apply + auto-rollback**.

## Why this exists

Skills install once and then *rot* — the upstream changes and your agent never
knows (see "Claude Code skills will rot unless teams track expiry dates"). This
client closes the loop: it reconciles your local skills dir against your Recipes
cookbook's declared state, and — critically — **a reconcile can never leave your
agent broken.** Every apply snapshots a last-known-good, verifies content hashes,
swaps atomically, runs a health check, and auto-reverts on any failure.

## The trust primitive (why it's safe to run unattended)

```
snapshot LKG → fetch only changed skills (sha256-delta) → verify hash
   → atomic swap (os.replace) → health check
        PASS → keep
        FAIL → AUTO-ROLLBACK to LKG, lockfile untouched, exit non-zero
```

This is *why GitOps beat manual kubectl*, ported to agent skills.

## Install (one command)

```bash
# 1. Set your Recipes API key (from recipes.wisechef.ai → settings → API keys)
export RECIPES_API_KEY=rec_live_xxxxxxxx

# 2. Run the installer — it auto-detects your host (Hermes/Codex/Claude),
#    wires a reconcile cron, and registers your cookbook.
bash scripts/install.sh --cookbook <YOUR_COOKBOOK_UUID>
```

That writes a `recipes-lock.json` next to your skills dir and adds a cron line
that runs `scripts/recipes-reconcile` every 30 minutes. Nothing else to manage —
the client even keeps *itself* up to date (it's a skill in your cookbook).

## Manual run (the "taste" — watch a cookbook self-heal once)

```bash
scripts/recipes-reconcile \
  --cookbook <YOUR_COOKBOOK_UUID> \
  --api https://recipes.wisechef.ai \
  --skills-dir ~/.hermes/skills \
  --lockfile ~/.hermes/recipes-lock.json
```

Output is JSON: `{"status": "applied", "applied": [...], "removed": [...]}` —
or `{"status": "up_to_date"}` when the 304-cheap path short-circuits (your
cookbook hasn't changed). Free tier includes one manual sync so you can watch a
skill update + auto-recover before deciding to put it on a schedule (Pro).

## Tiers

- **Free** — install + one cookbook + one manual sync (the taste).
- **Pro** — scheduled auto-reconcile (the cron). Skills never rot, hands-off.
- **Pro+** — fleet reconcile across many agents + canary/stable/frozen channels.

The paid axis is **maintenance, not access** — the catalog is free; what you pay
for is keeping deployed agents correct over time.

## What it touches

Only your skills dir (`~/.hermes/skills`, `~/.codex/skills`, etc.) and a
`recipes-lock.json` beside it. It writes nothing else, makes no inbound
connections, and uses your own API key. Pull-only by design.

## Flags

| Flag | Meaning |
|---|---|
| `--cookbook` | Cookbook UUID to keep evergreen (required) |
| `--api` | Recipes API base (default `https://recipes.wisechef.ai`) |
| `--skills-dir` | Your live skills directory (required) |
| `--lockfile` | Path to `recipes-lock.json` (required) |
| `--prune` | Allow REMOVE — uninstall skills dropped from the cookbook (opt-in) |
| `--api-key` | x-api-key (else `RECIPES_API_KEY` env) |
