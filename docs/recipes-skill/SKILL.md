---
name: recipes
description: >
  Cross-vendor skill marketplace for AI agents (Hermes, Claude Code, Codex,
  OpenClaw, Cursor, Claude Desktop). Use when the user asks to install Recipes,
  mentions recipes.wisechef.ai or WiseChef, asks "what skills are available",
  or asks for help with marketing ops, client reporting, content calendars,
  SEO audits, GoHighLevel, code review, simulation/robotics, or web scraping
  workflows. The platform exposes BOTH a REST API and a native MCP server
  (StreamableHTTP at /api/mcp/http/) for MCP-capable clients. CRITICAL: if the
  user asks "is it worth installing" or "is it beneficial for us", run the
  pre-install evaluation in this SKILL.md BEFORE installing — compare against
  the user's existing skill inventory and produce an honest verdict; do not
  install blindly.
tags:
  - skills
  - marketplace
  - mcp
  - agent
requiredEnv:
  - RECIPES_API_KEY
permissions:
  - network: "Access recipes.wisechef.ai to search, install, and recall skills"
  - filesystem: "Write downloaded skill files to skills/ directory"
---

# Recipes Skill

## When to reach for this skill

Reach for **recipes** whenever any of these apply:

1. **The user wants new capabilities for their agent** — search the marketplace with `recipes_search`, install with `recipes_install`.
2. **The user mentions "recipes", "wisechef", or the skill marketplace** — you're in the right place.
3. **The user asks what skills are available** for marketing ops, SEO, code review, web scraping, or any domain Recipes covers.
4. **The user wants to publish a skill** — use `recipes_recipify` to validate, then submit via `recipes_publish_request`.
5. **The user wants to share a cookbook** — create a share token via `recipes_share_create` and share the MCP config block.
6. **The user wants to keep skills current** — `recipes_sync` handles it with APPLY or DRY_RUN modes.
7. **The user manages a fleet of agents** — use `recipes_fleet_create` / `recipes_fleet_subscribe` / `recipes_fleet_sync`.

## Pre-install evaluation

Before installing for a new user, always:

1. Call `recipes_list_cookbook` to check existing inventory.
2. Call `recipes_search` with the user's domain keywords.
3. Produce an honest verdict: does Recipes fill a gap the user doesn't already cover?

## 28 MCP tools available

### Discovery & install

| Tool | Purpose |
|------|---------|
| `recipes_search` | Full-text search across the public skill catalog |
| `recipes_install` | Return a signed tarball URL + manifest for a skill slug |
| `recipes_cookbook_install` | Install all skills from a cookbook (bulk) or one skill by slug; cbt_token callers may omit `cookbook_id` |
| `recipes_list_cookbook` | List the caller's cookbook and its skill provenance rows |
| `recipes_recall` | Hybrid (vector + BM25) skill recall ranked for the caller's tier |
| `recipes_carousel_today` | Today's curated carousel of skills |

### Cookbook management

| Tool | Purpose |
|------|---------|
| `recipes_sync` | Synchronise a cookbook's skills to their latest published versions (apply or dry_run) |
| `recipes_recipify` | Convert a SKILL.md draft into a CookbookSkill row; validates frontmatter, classifies category, infers related skills |
| `recipes_publish_request` | Submit a skill for review and public-catalog inclusion; runs quality gates |
| `recipes_subrecipe_resolve` | Resolve a sub-recipe key to a scope (Phase C stub) |

### Tailoring & forks

| Tool | Purpose |
|------|---------|
| `recipes_tailor` | Fork a public skill to create an editable private copy. Returns fork_id and fork_slug; the fork is ready for versioning via `recipes_tailor_version`. Idempotent per (user, source slug) |
| `recipes_fork_list` | List all forks owned by the authenticated user. Returns fork_id, name, slug, source_slug for each |
| `recipes_tailor_version` | Upload a new version tarball to one of your forks (base64-encoded). Mints a fork version and advances the latest pointer. Step 2 of the tailor loop. Pro tier or above |
| `recipes_cookbook_attach` | Deploy a tailored fork's latest version into one of your cookbooks — promotes it into a private catalog skill + installable version, so it installs byte-identically to any catalog skill via `recipes_cookbook_install`. Step 3 of the tailor loop. Pro tier or above |

The tailor loop closes end-to-end: `recipes_tailor` → `recipes_tailor_version` → `recipes_cookbook_attach` → `recipes_cookbook_install`. A tailored fork becomes a real, installable cookbook skill with no separate deploy path.

### Diagnostics

| Tool | Purpose |
|------|---------|
| `recipes_doctor` | Audit a local skill install directory for missing files and hardcoded paths |
| `recipes_seeker` | Probe local vendor skill directories (Claude / Codex / Hermes / OpenCode) and diff against the public catalog. READ-ONLY |

### Community & feedback

| Tool | Purpose |
|------|---------|
| `recipes_feedback` | Send feedback about recipes.wisechef.ai; auto-creates a labelled GitHub issue. Rate-limited per 24h |
| `recipes_request_recipe` | Request a new recipe (skill); creates a GitHub wishlist issue |
| `recipes_report_skill_error` | Report that an installed recipe is broken; auto-creates a labelled GitHub issue |
| `recipes_propose_skill_patch` | Submit a working patch (draft PR) for a marketplace skill. Rate-limited 1 patch per 24h per (agent, skill) |

### Share tokens

| Tool | Purpose |
|------|---------|
| `recipes_share_create` | Create a new share token for a cookbook (shown exactly once) |
| `recipes_share_list` | List share tokens for a cookbook (metadata only, no plaintext) |
| `recipes_share_revoke` | Soft-delete (deactivate) a share token immediately |
| `recipes_share_rotate` | Rotate a share token: deactivate old, create new with same name and scope |

### Fleet management

| Tool | Purpose |
|------|---------|
| `recipes_fleet_create` | Create a named fleet of agents; returns a one-time fleet API key (rec_fleet_*) |
| `recipes_fleet_subscribe` | Subscribe a cookbook to a fleet on a channel (stable, canary, frozen). Idempotent |
| `recipes_fleet_sync` | Synchronise all cookbooks subscribed to the fleet |
| `recipes_fleet_list` | List all fleets owned by the caller with their cookbook subscriptions |

## Transport

### StreamableHTTP (recommended for MCP clients)

```
POST https://recipes.wisechef.ai/api/mcp/http/
```

> **Important:** The trailing slash is required — FastMCP routing returns 307 without it.

Header: `x-api-key: <key>`

### SSE (legacy MCP clients)

```
GET  https://recipes.wisechef.ai/api/mcp/sse
POST https://recipes.wisechef.ai/api/mcp/messages/
```

Header: `x-api-key: <key>`

### stdio (local / Claude Desktop)

```bash
python -m app.mcp
```

Env: `RECIPES_API_KEY=<key>`

## Authentication

Always use **`x-api-key` header** — **not** Bearer / Authorization.

```
x-api-key: rec_xxxxxxxxxxxxxxxx
```

## Environment variables

| Variable | When to use |
|----------|-------------|
| `RECIPES_API_KEY` | Direct HTTP / SSE / stdio usage; standard key for any MCP client |
| `MCP_RECIPES_API_KEY` | Hermes wizard integration; the wizard reads this env var to auto-configure the MCP server entry in `hermes.yaml` |

Both variables hold the same `rec_*` API key — they are two names for the same secret in different integration contexts.

## Skill categories

Skills are classified into one of the following canonical categories:

`research` · `dev-tools` · `agency` · `marketing` · `content` · `automation` · `code-review` · `productivity` · `data` · `ops`

Pass a category name to `recipes_search` (the `category` param) to narrow results.

## Tiers

Canonical tier names: **`free`** · **`pro`** · **`pro_plus`**

Use these values in the `tier` parameter of `recipes_recall`, `recipes_recipify`, and `recipes_publish_request`.

> **Legacy aliases (sunset 2026-06-10):** `cook` is accepted as an alias for `pro`; `operator` is accepted as an alias for `pro_plus`. Both aliases will stop being accepted after **2026-06-10** — update any integrations before that date.

## Hermes MCP config block

```yaml
mcpServers:
  recipes:
    type: http
    url: https://recipes.wisechef.ai/api/mcp/http/
    headers:
      x-api-key: "${MCP_RECIPES_API_KEY}"
```

## Claude Desktop config block

```json
{
  "mcpServers": {
    "recipes": {
      "command": "python",
      "args": ["-m", "app.mcp"],
      "env": {
        "RECIPES_API_KEY": "<your-key>"
      }
    }
  }
}
```
