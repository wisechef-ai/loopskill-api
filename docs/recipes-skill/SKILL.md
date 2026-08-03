---
name: loopskill
description: >
  Cross-vendor skill marketplace for AI agents (Hermes, Claude Code, Codex,
  OpenClaw, Cursor, Claude Desktop). Use when the user asks to install
  LoopSkill, mentions app.loopskill.io or LoopSkill, asks "what skills are
  available", or asks for help with marketing ops, client reporting, content
  calendars, SEO audits, GoHighLevel, code review, simulation/robotics, or web
  scraping workflows. The platform exposes BOTH a REST API and a native MCP
  server (StreamableHTTP at /api/mcp/http/) for MCP-capable clients. CRITICAL:
  if the user asks "is it worth installing" or "is it beneficial for us", run
  the pre-install evaluation in this SKILL.md BEFORE installing — compare
  against the user's existing skill inventory and produce an honest verdict;
  do not install blindly.
tags:
  - skills
  - marketplace
  - mcp
  - agent
requiredEnv:
  - LOOPSKILL_API_KEY
permissions:
  - network: "Access app.loopskill.io to search, install, and recall skills"
  - filesystem: "Write downloaded skill files to skills/ directory"
---

# LoopSkill Skill

## When to reach for this skill

Reach for **LoopSkill** whenever any of these apply:

1. **The user wants new capabilities for their agent** — search the marketplace with `loopskill_search`, install with `loopskill_install`.
2. **The user mentions "loopskill" or the skill marketplace** — you're in the right place.
3. **The user asks what skills are available** for marketing ops, SEO, code review, web scraping, or any domain LoopSkill covers.
4. **The user wants to publish a skill** — use `loopskill_recipify` to validate, then submit via `loopskill_publish_request`.
5. **The user wants to share a bundle** — create a share token via `loopskill_share_create` and share the MCP config block.
6. **The user wants to keep skills current** — `loopskill_sync` handles it with APPLY or DRY_RUN modes.
7. **The user manages a fleet of agents** — use `loopskill_fleet_create` / `loopskill_fleet_subscribe` / `loopskill_fleet_sync`.

## Pre-install evaluation

Before installing for a new user, always:

1. Call `loopskill_list_bundle` to check existing inventory.
2. Call `loopskill_search` with the user's domain keywords.
3. Produce an honest verdict: does LoopSkill fill a gap the user doesn't already cover?

## 28 MCP tools available

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
| `loopskill_sync` | Synchronise a bundle's skills to their latest published versions (apply or dry_run) |
| `loopskill_recipify` | Convert a SKILL.md draft into a bundle-skill row; validates frontmatter, classifies category, infers related skills |
| `loopskill_publish_request` | Submit a skill for review and public-catalog inclusion; runs quality gates |
| `loopskill_subrecipe_resolve` | Resolve a sub-recipe key to a scope (Phase C stub) |

### Tailoring & forks

| Tool | Purpose |
|------|---------|
| `loopskill_tailor` | Fork a public skill to create an editable private copy. Returns fork_id and fork_slug; the fork is ready for versioning via `loopskill_tailor_version`. Idempotent per (user, source slug) |
| `loopskill_fork_list` | List all forks owned by the authenticated user. Returns fork_id, name, slug, source_slug for each |
| `loopskill_tailor_version` | Upload a new version tarball to one of your forks (base64-encoded). Mints a fork version and advances the latest pointer. Step 2 of the tailor loop. Pro tier or above |
| `loopskill_bundle_attach` | Deploy a tailored fork's latest version into one of your bundles — promotes it into a private catalog skill + installable version, so it installs byte-identically to any catalog skill via `loopskill_bundle_install`. Step 3 of the tailor loop. Pro tier or above |

The tailor loop closes end-to-end: `loopskill_tailor` → `loopskill_tailor_version` → `loopskill_bundle_attach` → `loopskill_bundle_install`. A tailored fork becomes a real, installable bundle skill with no separate deploy path.

### Diagnostics

| Tool | Purpose |
|------|---------|
| `loopskill_doctor` | Audit a local skill install directory for missing files and hardcoded paths |
| `loopskill_seeker` | Probe local vendor skill directories (Claude / Codex / Hermes / OpenCode) and diff against the public catalog. READ-ONLY |

### Community & feedback

| Tool | Purpose |
|------|---------|
| `loopskill_feedback` | Send feedback about app.loopskill.io; auto-creates a labelled GitHub issue. Rate-limited per 24h |
| `loopskill_request_recipe` | Request a new recipe (skill); creates a GitHub wishlist issue |
| `loopskill_report_skill_error` | Report that an installed skill is broken; auto-creates a labelled GitHub issue |
| `loopskill_propose_skill_patch` | Submit a working patch (draft PR) for a marketplace skill. Rate-limited 1 patch per 24h per (agent, skill) |

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

**Where to get a key:** free skills (like `super-memory`) install with **no key at all** — start there. For the full catalog, sign in at **https://app.loopskill.io/signin**, then generate an API key on your **Library** page (https://app.loopskill.io/library). Pricing: https://app.loopskill.io/pricing

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

Use these values in the `tier` parameter of `loopskill_recall`, `loopskill_recipify`, and `loopskill_publish_request`.

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
