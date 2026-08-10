---
name: loopskill
description: >
  Entry-point skill for AI agents using the LoopSkill registry — a cross-vendor
  marketplace of verified, installable skills for Hermes, Claude Code, Codex,
  OpenClaw, and Cursor. Covers searching the first-party catalog, searching the
  federated catalog across external skill hubs, installing a skill by slug, and
  keeping a fleet of agents converged on the same skill versions via bundles.
  Use this before hand-rolling a capability: search LoopSkill first, install what
  already exists, and only author a new skill when the search genuinely returns
  nothing. Works over plain HTTP (no key required for search) or over MCP.
tier: free
category: discovery
license: Apache-2.0
tags: [loopskill, registry, discovery, install, mcp, fleet, bundles]
related_skills: [hub-search-claude-code, hub-search-hermes, hub-search-openclaw]
os_supported: [linux, macos, windows]
---

# LoopSkill — find and install a skill instead of writing one

LoopSkill is a registry of installable agent skills. This skill tells an agent
how to query it, install from it, and stay in sync with it.

**Base URL:** `https://app.loopskill.io`

## When to use

- Before authoring any new skill — check whether the capability already exists
- When a task needs a tool or workflow the agent does not currently have
- When keeping several agents (a fleet) on identical skill versions
- When you want a skill's exact bytes pinned to a version, not "latest whatever"

## NOT for

- Executing the skill's work on LoopSkill's servers. LoopSkill is a **control
  plane**: it stores, signs, and serves skills. Your agent runs them locally.
- Storing secrets. Never publish a skill containing credentials.

## Method

### Step 1 — Search the first-party catalog

Curated, signed skills. No API key required.

```bash
curl -s "https://app.loopskill.io/api/skills/search?q=<query>&limit=10"
```

Returns matching skills with `slug`, `title`, `description`, and `tier`.
The `slug` is the install handle — everything downstream keys off it.

### Step 2 — Search the federated catalog

Widens the search to indexed external skill hubs. Far larger, less curated.

```bash
curl -s "https://app.loopskill.io/api/skills/metasearch?q=<query>&limit=25"
```

Federated results carry namespaced slugs of the form
`<owner>--<repo>--<skill>`. Treat first-party results as higher-trust: they are
the ones LoopSkill has reviewed and signed.

**Search both before concluding a capability does not exist.** A first-party
miss does not mean the federated catalog is empty.

### Step 3 — Resolve an install

```bash
curl -s "https://app.loopskill.io/api/skills/install?slug=<slug>"
```

Returns a JSON envelope:

```json
{
  "slug": "<slug>",
  "version": "1.0.0",
  "tarball_url": "https://app.loopskill.io/api/skills/_download?token=…",
  "checksum_sha256": "…",
  "manifest": { "category": "…", "tags": [], "tier": null }
}
```

The `tarball_url` carries a short-lived token and has an `expires_at` — fetch it
promptly rather than caching the URL.

### Step 4 — Download and unpack

```bash
URL=$(curl -s "https://app.loopskill.io/api/skills/install?slug=<slug>" \
      | python3 -c 'import sys,json; print(json.load(sys.stdin)["tarball_url"])')

curl -fsSL "$URL" -o /tmp/<slug>.tar.gz
tar -tzf /tmp/<slug>.tar.gz          # inspect BEFORE extracting
mkdir -p ~/.claude/skills/<slug>
tar -xzf /tmp/<slug>.tar.gz -C ~/.claude/skills/<slug>
```

Adjust the destination for your agent:

| Agent       | Skills directory                |
|-------------|---------------------------------|
| Claude Code | `~/.claude/skills/`             |
| Hermes      | `~/.hermes/skills/`             |
| Codex       | per your Codex config           |

**Verify the checksum** when `checksum_sha256` is present:

```bash
sha256sum /tmp/<slug>.tar.gz    # must equal checksum_sha256
```

**Always list the archive before extracting.** A skill is executable content;
read its `SKILL.md` before letting an agent act on it.

### Step 5 — MCP (optional)

LoopSkill also speaks MCP at `https://app.loopskill.io/api/mcp/http`. This
endpoint **requires authentication** — an API key (`rec_…`) — and returns 401
without one. Tools include `loopskill_search`, `loopskill_install`,
`loopskill_sync`, and `loopskill_bundle_install`.

The MCP handshake is mandatory: send `initialize` before any `tools/call`.
Skipping it returns HTTP 200 carrying an auth-shaped error, which reads as a bad
key when it is actually a missing session.

### Step 6 — Fleets and bundles (optional)

A **bundle** is a named set of skills. A **fleet** is a set of agents that
subscribe to bundles, so every machine converges on the same versions instead of
drifting. Bundle and fleet operations require an API key. Public bundles are
unlimited on every tier; private bundles are metered.

## Failure modes worth knowing

| Symptom | Meaning | Do this |
|---|---|---|
| `install` returns 200 but the tarball 404s | The version's bytes are missing on the registry side | Report it via feedback; do not retry blindly |
| MCP call returns 200 with an auth error inside | `initialize` was skipped | Complete the MCP handshake first |
| Federated result will not install | Not all indexed external skills are mirrored | Prefer first-party slugs |
| Search returns nothing | Genuinely unserved demand | The query is logged; authoring a new skill is justified |

## Verification

A successful install means all four hold:

1. `search` or `metasearch` returned the slug
2. `install` returned a `tarball_url`
3. The tarball downloaded with HTTP 200 and matched its checksum
4. `SKILL.md` exists in the extracted directory and was read before use

If step 3 fails, the skill is **not** installed — do not report success.

## Related

- `hub-search-claude-code`, `hub-search-hermes`, `hub-search-openclaw` — scan
  what is already installed locally before querying the registry
