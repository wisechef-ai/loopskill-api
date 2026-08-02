# Your skills never rot — LoopSkill as your agent's control plane

> **Status:** shipped. The reconcile client is live in the catalog as
> `recipes-cookbook-reconcile` (legacy name, still works), and the cold-path in
> this guide is verified end-to-end (a fresh agent reproduces install → cookbook
> → sync with only the public surfaces linked here).

Skills install once and then **rot**. You add a skill to your agent, the upstream
author improves it next week, and your agent never finds out — it's running a
stale copy, silently drifting toward outdated conventions. LoopSkill fixes this:
hand your agent a **bundle**, and its skills stay evergreen automatically, with
a safety guarantee no other skill platform offers — **a reconcile can never leave
your agent broken.**

## The 60-second version

```bash
# 1. Get your API key at loopskill.io → settings → API keys
export RECIPES_API_KEY=rec_li...n  # TODO(rename): env var still uses legacy name for prod compatibility

# 2. Install LoopSkill itself (self-hosted; skip if you already have an
#    instance running):
#    curl -fsSL loopskill.io/install | sh
#    (or, from a clone of this repo: ./install.sh)

# 3. Run one reconcile cycle by hand — see "watch it work" below.
```

Want to *watch* it work before trusting a cron? Run one sync by hand (free tier
includes one):

```bash
python -m app.reconcile_cli \
  --cookbook <YOUR_BUNDLE_UUID> \
  --api https://loopskill.io \
  --skills-dir ~/.hermes/skills \
  --lockfile ~/.loopskill/state/lock.json
```

You'll see a skill update **and auto-recover** in front of you. That's the taste.

## Why it's safe to run unattended

The reconcile client is built on one principle — **atomic apply + auto-rollback**:

```
snapshot last-known-good → pull only changed skills (sha256 delta, CDN-served)
   → verify each hash → atomic swap (os.replace) → health check
        PASS → keep the update
        FAIL → AUTO-ROLLBACK to the snapshot; your agent is untouched
```

If a new skill version is broken — empty file, corrupt frontmatter, fails its
health check — the client reverts to the exact state it started in and records
the failure. Your agent never runs the broken version. This is *why GitOps beat
manual deploys*, ported to agent skills.

It's also **fast and cheap at scale**: the client polls with a conditional
request, so when your bundle hasn't changed the server answers in a single
indexed lookup (HTTP 304) and the client does nothing. When it *has* changed,
only the skills whose content hash moved are pulled — and those come from
Cloudflare's edge, not the origin. Thousands of agents reconcile on one modest
server.

## What you pay for: maintenance, not access

The catalog is free. What LoopSkill charges for is **keeping your deployed agents
correct over time**:

| Tier | What you get |
|---|---|
| **Free** | Install any skill + one bundle + **one manual sync** (the taste) |
| **Pro** | **Scheduled auto-reconcile** — skills never rot, fully hands-off |
| **Pro+** | **Fleet reconcile** across many agents + canary / stable / frozen channels |

The **channels** are how teams ship safely: a new skill version lands on
**canary** agents first, and only promotes to **stable** after it passes a health
gate there. A bad version that breaks a canary agent is **blocked** from ever
reaching your production fleet. "Agents that execute without errors" isn't a hope
— it's a mechanism.

## How it stays current (including itself)

The reconcile client ships **as a skill inside the bundle it manages**, so it
updates itself through the same loop. There is no fat daemon to maintain, no
separate version to drift. The intelligence lives server-side (the reconcile
engine); the host-side piece is a thin, self-contained trigger that rides your
existing scheduler. Pull-only — it uses your own API key and accepts no inbound
connections.

## Cold-path guarantee

Everything in this guide was validated as a **cold agent** — a fresh install
using only the public surfaces linked here, no insider shortcuts. The
`recipes-cookbook-reconcile` bundle is self-contained (standard library only):
install it, point it at your bundle, and your skills are evergreen.

Start at [loopskill.io](https://loopskill.io).
