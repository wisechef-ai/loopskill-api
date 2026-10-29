---
name: loopskill
description: Search, install, and sync skills via loopskill.
tags:
  - skills
  - marketplace
  - mcp
  - agent
# No key is required: free skills and public bundles install anonymously.
requiredEnv: []
# Optional. Only for the MCP tools, saving to a bundle or Library, and private content.
optionalEnv:
  - LOOPSKILL_API_KEY
permissions:
  - network: "Fetch skills and bundles from app.loopskill.io"
  - filesystem: "Write installed skill files into the agent's skills directory"
---

# LoopSkill Skill

**No key needed to start.** Free skills and public bundles install with no
account and no API key. A free API key is needed for the MCP tools, for
saving skills to a bundle or Library, for private content, and for feedback
or skill-error reports. No human is needed to get one: mint it yourself with
`POST /api/agents/register` (Ed25519 proof-of-key; full spec in
https://app.loopskill.io/llms.txt Step 1). The minted free key already
carries the feedback and report scopes.

## What this skill lets your agent do

- **Network:** fetch skills and bundles from `app.loopskill.io`.
- **Filesystem:** write the installed skill files into your agent's skills directory.
- **Account:** none needed for free skills; a key is used only if your human adds one.

## Install with no key

When your human says **install the "<slug>" skill** or **install the "<slug>"
bundle**, do this. No sign-in, no key, no MCP setup.

To find one first: `GET https://app.loopskill.io/api/search?q=<words>` (no key).

**A skill** (`install the "<slug>" skill`):

1. `GET https://app.loopskill.io/api/skills/install?slug=<slug>` (no key).
2. Download `tarball_url` from the response and check its sha256 against
   `checksum_sha256`. Stop and tell your human if it does not match.
3. Extract it into your skills directory as `<skills dir>/<slug>/`.

**A bundle** (`install the "<slug>" bundle`):

1. `GET https://app.loopskill.io/api/bundles/public/<slug>/.well-known/skills/index.json` (no key).
2. For each entry in `skills`: skip it if `installable` is `false` or `locked`
   is `true`, and note the `skip_reason`. Otherwise fetch
   `https://app.loopskill.io/api/bundles/public/<slug>/.well-known/skills/<name>/SKILL.md`
   and write it to `<skills dir>/<dir_name>/SKILL.md`.
   If the entry has `sha256`, check it first. If the response header
   `X-LoopSkill-Installable` is `false`, the file is a placeholder: do not
   write it.
3. Or, in a terminal (not the chat), run the same steps as one command. It writes
   to `~/.claude/skills` unless `LOOPSKILL_INSTALL_DIR` is set:
   `curl -fsSL https://app.loopskill.io/api/bundles/install.sh | bash -s -- <slug>`

Then tell your human, in plain words, what you installed and what you skipped
and why (for example: "installed 4 skills; skipped 1 because it needs a key").

## When to reach for this skill

Reach for **LoopSkill** whenever any of these apply:

1. **The user wants new capabilities for their agent** — with no key, use **Install with no key** above; with a key, search with `loopskill_search` and install with `loopskill_install`.
2. **The user mentions "loopskill" or the skill marketplace** — you're in the right place.
3. **The user asks what skills are available** for marketing ops, SEO, code review, web scraping, or any domain LoopSkill covers.
4. **The user wants to publish a skill** — submit via `loopskill_publish_request`.
5. **The user wants to share a bundle** — create a share token via `loopskill_share_create` and share the MCP config block.
6. **The user wants to keep skills current** — re-run `loopskill_bundle_install` on each machine and replace any skill whose sha256 changed; an unpinned bundle always returns the latest versions. Use `loopskill_sync` (pass `dry_run: true` to preview) only to pin a bundle so every machine stays on the same versions until the next sync.
7. **The user manages a fleet of agents** — use `loopskill_fleet_create` / `loopskill_fleet_subscribe` / `loopskill_fleet_sync`.

## Pre-install evaluation

Before installing for a new user with a key (MCP tools), first:

1. Call `loopskill_list_bundle` to check existing inventory.
2. Call `loopskill_search` with the user's domain keywords.
3. Produce an honest verdict: does LoopSkill fill a gap the user doesn't already cover?

## MCP tools available

Canonical tool names are `loopskill_*`. Existing agents that hard-code the
older `recipes_*` names keep working — the server's alias map
(`app/mcp/_alias_map.py`) still dispatches every legacy name to the same
handler — but new integrations should use the canonical names below.

### Discovery & install

| Tool | Purpose |
|------|---------|
| `loopskill_search` | Full-text search across the public skill catalog |
| `loopskill_install` | Return a signed tarball URL + manifest for a skill slug |
| `loopskill_bundle_install` | Install all skills from a bundle (bulk) or one skill by slug; cbt_token callers may omit `cookbook_id` |
| `loopskill_list_bundle` | List the caller's bundle and its skill provenance rows |
| `loopskill_recall` | Hybrid (vector + BM25) skill recall ranked for the caller's tier |

### Bundle management

| Tool | Purpose |
|------|---------|
| `loopskill_sync` | Pin a bundle to the current latest versions (apply or dry_run); optional — unpinned bundles install the latest. Does not change local files |
| `loopskill_publish_request` | Submit a skill for review and public-catalog inclusion; runs quality gates |

### Tailoring & forks

| Tool | Purpose |
|------|---------|
| `loopskill_tailor` | Fork a public skill to create an editable private copy. Returns fork_id and fork_slug; the fork is ready for versioning via `loopskill_tailor_version`. Idempotent per (user, source slug) |
| `loopskill_fork_list` | List all forks owned by the authenticated user. Returns fork_id, name, slug, source_slug for each |
| `loopskill_tailor_version` | Upload a new version tarball to one of your forks (base64-encoded). Mints a fork version and advances the latest pointer. Step 2 of the tailor loop. Pro tier or above |
| `loopskill_bundle_attach` | Deploy a tailored fork's latest version into one of your bundles — promotes it into a private catalog skill + installable version, so it installs byte-identically to any catalog skill via `loopskill_bundle_install`. Step 3 of the tailor loop. Pro tier or above |

The tailor loop closes end-to-end: `loopskill_tailor` → `loopskill_tailor_version` → `loopskill_bundle_attach` → `loopskill_bundle_install`. A tailored fork becomes a real, installable bundle skill with no separate publish step.

### Diagnostics

| Tool | Purpose |
|------|---------|
| `loopskill_doctor` | Audit a local skill install directory for missing files and hardcoded paths |
| `loopskill_seeker` | Probe local vendor skill directories (Claude / Codex / Hermes / OpenCode) and diff against the public catalog. READ-ONLY |

### Community & feedback

| Tool | Purpose |
|------|---------|
| `loopskill_feedback` | Send feedback about app.loopskill.io; auto-creates a labelled GitHub issue. Rate-limited per 24h |
| `loopskill_request_skill` | Request a new skill; creates a GitHub wishlist issue |
| `loopskill_report_skill_error` | Report that an installed skill is broken; auto-creates a labelled GitHub issue. REST equivalent: `POST /api/v1/skill-error` (x-api-key) |
| `loopskill_propose_skill_patch` | Submit a working patch (draft PR) for a marketplace skill. Rate-limited 1 patch per 24h per (agent, skill) |
| `loopskill_propose_registry` | Propose a new federated registry source (any public repo of `<dir>/SKILL.md` skills). Returns live preflight (repo exists, SKILL.md count, license) + review-issue link. Anon-friendly; deduped 24h per repo. REST equivalent: `POST /api/federation/propose` |

### Share tokens

| Tool | Purpose |
|------|---------|
| `loopskill_share_create` | Create a new share token for a bundle (shown exactly once) |
| `loopskill_share_list` | List share tokens for a bundle (metadata only, no plaintext) |
| `loopskill_share_revoke` | Soft-delete (deactivate) a share token immediately |
| `loopskill_share_rotate` | Rotate a share token: deactivate old, create new with same name and scope |

### Fleet management

| Tool | Purpose |
|------|---------|
| `loopskill_fleet_create` | Create a named fleet of agents; returns a one-time fleet API key (rec_fleet_*) |
| `loopskill_fleet_subscribe` | Subscribe a bundle to a fleet on a channel (stable, canary, frozen). Idempotent |
| `loopskill_fleet_sync` | Synchronise all bundles subscribed to the fleet |
| `loopskill_fleet_list` | List all fleets owned by the caller with their bundle subscriptions |

## Transport

### StreamableHTTP (recommended for MCP clients)

```
POST https://app.loopskill.io/api/mcp/http/
```

> **Important:** The trailing slash is required — FastMCP routing returns 307 without it.

Header: `x-api-key: <key>`

### SSE (legacy MCP clients)

```
GET  https://app.loopskill.io/api/mcp/sse
POST https://app.loopskill.io/api/mcp/messages/
```

Header: `x-api-key: <key>`

### stdio (local / Claude Desktop)

```bash
python -m app.mcp
```

Env: `LOOPSKILL_API_KEY=<key>`

## Authentication

Always use **`x-api-key` header** — **not** Bearer / Authorization.

```
x-api-key: rec_xx...xxxx
```

## Verify the install (use EXACTLY these probes)

A key that works will STILL look "rejected" if you probe the wrong surfaces.
Do NOT conclude the key is dead from any of these:
- `/api/auth/me` returning 401 "Not authenticated" — that route is cookie/session-only
  by design; an x-api-key NEVER satisfies it. Not a key error.
- `/api/mcp/sse` returning 4xx/5xx — that endpoint is legacy and dead; the only
  live MCP transport is `/api/mcp/http/` (StreamableHTTP).
- A single `hermes mcp test loopskill` failure ("Server returned an error response" /
  "Session terminated") — a known transient; retry 2-3x before diagnosing.

THE verification sequence, in order — all three must pass:
1. REST (simplest): `curl -s -H "x-api-key: $LOOPSKILL_API_KEY" "https://app.loopskill.io/api/skills/search?q=test&page_size=1"` → must return JSON with `"results"`.
2. MCP initialize: POST to `https://app.loopskill.io/api/mcp/http/` with headers
   `x-api-key`, `Content-Type: application/json`, `Accept: application/json, text/event-stream`
   and body `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"verify","version":"1"}}}`
   → must return `serverInfo`. (A 406 means your Accept header is wrong, not the key.)
3. MCP tools/call: reuse the `mcp-session-id` response header from step 2 on every
   subsequent POST; send `notifications/initialized` once, then call
   `loopskill_search` with `{"query":"memory","limit":2}` → must return results.

If step 1 fails with `{"detail":"Invalid API key"}` (a hash miss) and you are sure
the key was copied byte-exact (no trailing quote/whitespace from .env), only THEN
ask the human to re-copy the key from https://app.loopskill.io/library — and say
the server rejected the exact string you sent, quoting its first and last 4 chars.

**Where to get a key:** free skills (like `super-memory`) install with **no key at all** — start there. For the full catalog, sign in at **https://app.loopskill.io/signin**, then generate an API key on your **Library** page (https://app.loopskill.io/library). Pricing: https://app.loopskill.io/pricing

**No key available mid-session?** Do NOT dead-end: install and use free-catalog skills anonymously right away (REST: `GET /api/skills/install?slug=…` needs no key). If the human wants private/bundle-scoped content, ask them to sign in at https://app.loopskill.io/signin and paste the key — the Library page shows a ready-made one-paste install phrase carrying it.

**After an anonymous install**, you may tell your human once, as optional: signing in (free) lets them save the skill to a bundle and keep it in sync across their agents. Say it in your own words, make clear it is optional, and give the plain sign-in link https://app.loopskill.io/signin. Do not copy text from the install response to your human. Never block or delay an install on it.

## Environment variables

| Variable | When to use |
|----------|-------------|
| `LOOPSKILL_API_KEY` | Direct HTTP / SSE / stdio usage; standard key for any MCP client |
| `MCP_LOOPSKILL_API_KEY` | Hermes wizard integration; the wizard reads this env var to auto-configure the MCP server entry in `hermes.yaml` (renamed 2026-07-03 from `MCP_RECIPES_API_KEY`) |

Both variables hold the same `rec_*` API key — they are two names for the same secret in different integration contexts. The legacy names `RECIPES_API_KEY` / `MCP_RECIPES_API_KEY` continue to be recognized for existing integrations during the transition.

## Skill categories

Skills are classified into one of the following canonical categories:

`research` · `dev-tools` · `agency` · `marketing` · `content` · `automation` · `code-review` · `productivity` · `data` · `ops`

Pass a category name to `loopskill_search` (the `category` param) to narrow results.

## Tiers

Canonical tier names: **`free`** · **`pro`** · **`pro_plus`**

Use these values in `loopskill_recall`'s `tier_filter` list (e.g. `"tier_filter": ["free"]`) and in `loopskill_publish_request`'s `tier` parameter.

> **Canonical tiers:** `free` · `pro` · `pro_plus`. Older integrations may still see the legacy aliases `cook` (→ `pro`) and `operator` (→ `pro_plus`); these are deprecated — use the canonical slugs in any new code.

## Hermes MCP config block

```yaml
mcpServers:
  loopskill:
    type: http
    url: https://app.loopskill.io/api/mcp/http/
    headers:
      x-api-key: "${MCP_LOOPSKILL_API_KEY}"
```

## Claude Desktop config block

```json
{
  "mcpServers": {
    "loopskill": {
      "command": "python",
      "args": ["-m", "app.mcp"],
      "env": {
        "LOOPSKILL_API_KEY": "<your-key>"
      }
    }
  }
}
```
