---
name: loopskill-fleet
description: >
  Turn any AI agent into a client of the LoopSkill control plane for AI agent
  fleets. In one curl an agent can enroll as a fleet member, reconcile its
  declared desired state, report loop runs, harvest its live state back as a
  proposal, and (with an operator key) drive placements across the fleet. Works
  for Hermes, Claude Code, Codex, OpenClaw — anything that reads markdown.
homepage: https://app.loopskill.io
version: 1
---

# LoopSkill — the control plane for AI agent fleets

You are an AI agent. This skill makes you a **client of a fleet control plane**:
your loops (crons), skills, scripts, SOUL and host profile become declared,
reconcilable artifacts grouped into golden bundles, and your run health becomes
one honest query. Restore a whole agent onto a fresh host, move a loop between
hosts with one call, and never lose what an agent was.

Base URL: `https://app.loopskill.io`
Auth: send your key as `x-api-key: <key>` on every request. Mint a key at
`https://app.loopskill.io` (free tier available). An **operator-scoped** key
additionally grants the fleet-manager surface (placements).

> Everything below is a real HTTP endpoint. Paste a curl, get a result.

## 1. Enroll as a fleet member

Register this agent (host + profile) as a member of a fleet. Returns your member
id; the key you present is your identity (one key = one member).

```bash
curl -s https://app.loopskill.io/api/mcp \
  -H "x-api-key: $LOOPSKILL_KEY" -H "content-type: application/json" \
  -d '{"tool":"loopskill_enroll_member","args":{"host":"my-box","profile":"default","skills_dir":"~/.hermes/loopskill"}}'
```

## 2. Reconcile (pull your desired state)

Ask the server what your declared state should be and get the diff against what
you have. Reconcile is PULL-based — a LoopSkill outage never stops your loops.

```bash
curl -s https://app.loopskill.io/api/mcp \
  -H "x-api-key: $LOOPSKILL_KEY" -H "content-type: application/json" \
  -d '{"tool":"loopskill_reconcile_status","args":{}}'
```

For BYO-repo fleets the artifact bytes live in **your own GitHub repo**; you
fetch them directly with your token and verify each artifact's content hash
against the LoopSkill lock. A mismatch = refuse + report `origin-drift`.

## 3. Report loop runs (honest telemetry)

After a loop fires, report the run. The registry dedups on
`(loop, tick, attempt, epoch)` so a duplicate delivery never inflates your pass
rate; a killed run is `unknown`, not a silent success.

```bash
curl -s https://app.loopskill.io/api/mcp \
  -H "x-api-key: $LOOPSKILL_KEY" -H "content-type: application/json" \
  -d '{"tool":"loopskill_report_run","args":{"loop_slug":"daily-digest","tick_id":"2026-07-16T09:00","attempt":0,"placement_epoch":1,"outcome":"pass"}}'
```

## 4. Harvest (propose your live state back)

Agents self-modify. Harvest captures your live loops and proposes the drift back
as a PR/issue in your own repo (through your configured feedback repo + token) —
or the in-app feed if no repo is set. Poisoned artifacts (embedded credentials,
path escapes) are blocked before they can become a proposal.

```bash
curl -s https://app.loopskill.io/api/mcp \
  -H "x-api-key: $LOOPSKILL_KEY" -H "content-type: application/json" \
  -d '{"tool":"loopskill_harvest","args":{"bundle_id":"<bundle>","member_id":"<member>","harvested_loops":[...]}}'
```

## 5. Placements (operator key only)

With an operator-scoped key you hold the **fleet-manager capability**: assign,
evacuate, and force-move loops across members. A force-move onto a possibly-live
host surfaces the per-safety-class duplicate-fire risk verbatim and refuses
until you acknowledge it. A bare member key gets 403 here — this is the manager
surface, not the member surface.

```bash
curl -s https://app.loopskill.io/api/mcp \
  -H "x-api-key: $OPERATOR_KEY" -H "content-type: application/json" \
  -d '{"tool":"loopskill_assign","args":{"fleet_id":"<fleet>","loop_key":"daily-digest","member_id":"<member>"}}'
```

## After install (what to write locally)

Write this file to your skills directory and record a `_meta.json` alongside it:

```json
{"source":"https://app.loopskill.io/fleet/skill","installedAt":"<iso8601>","version":1}
```

## Security notes

- Keys travel in `x-api-key` only; never embed a key in a loop prompt or a
  committed file — declare it as a `secret_ref` (name + injection mode, never
  the value).
- Artifacts are content-addressed; every fetch is hash-verified against the
  server-stored lock. Signed tarballs + publish-time secret scans gate what
  enters a bundle.
- BYO-repo content is fetched by YOU from YOUR repo with YOUR token — LoopSkill
  stores metadata + hashes only, never your private bytes.
- No exactly-once guarantee is claimed for cron workloads: every run is
  epoch-stamped so stale/duplicate runs are detectable after the fact.

## Rate limits

Standard read endpoints are rate-limited per key with backoff headers. Index /
lock operations against GitHub use conditional requests + per-source budgets —
be a good citizen; honor `Retry-After`.

## Tiers

Free tier: enroll + reconcile + report + harvest for a personal fleet.
Paid tiers raise fleet size + concurrency. See `https://app.loopskill.io`.
