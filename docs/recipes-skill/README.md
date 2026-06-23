# Recipes — The Skill Marketplace for AI Agents

**Give your agent superpowers. Search, install, and run curated skills — in under 60 seconds.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

---

## The 60-Second Pitch

You're an AI agent (or you operate one). You want **composable, trusted capabilities** — not brittle API glue. Recipes is the marketplace where human-reviewed skills live. One MCP connection gives your agent 10+ tools for search, install, recall, diagnostics, and more. No dependencies. No vendor lock-in. Just skills that work.

**Publishers** earn recurring revenue via usage-attributed Stripe Connect payouts. **Subscribers** get auto-updating skills with zero config drift. **Teams** share private cookbooks with a single CLI command.

---

## Quick Install

### Hermes (StreamableHTTP)

Add to `~/.hermes/config.yaml`:

```yaml
mcpServers:
  recipes:
    transport: streamable-http
    url: https://recipes.wisechef.ai/api/mcp/http
    headers:
      x-api-key: YOUR_API_KEY
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "recipes": {
      "type": "streamable-http",
      "url": "https://recipes.wisechef.ai/api/mcp/http",
      "headers": {
        "x-api-key": "YOUR_API_KEY"
      }
    }
  }
}
```

### Codex CLI

Set in your environment:

```bash
export RECIPES_API_KEY=YOUR_API_KEY
# Then reference in your Codex MCP config pointing to:
# https://recipes.wisechef.ai/api/mcp/http
```

> Get your API key at [recipes.wisechef.ai/signin](https://recipes.wisechef.ai/signin) — free tier available.

---

## Quickstarts

| Guide | Time | What you'll do |
|-------|------|----------------|
| [Publisher quickstart](./QUICKSTART-publisher.md) | 5 min | Publish your first skill to the marketplace |
| [Subscriber quickstart](./QUICKSTART-subscriber.md) | 5 min | Install + auto-update your first skill |
| [Cookbook sharing](./QUICKSTART-share.md) | 3 min | Share a private cookbook with another agent |

---

## The 10 MCP Tools

Once connected, your agent gets these tools — no extra configuration:

| Tool | What it does |
|------|-------------|
| `recipes_search` | BM25 + semantic search across all marketplace skills |
| `recipes_install` | Install a skill into your agent's workspace |
| `recipes_list_cookbook` | List all cookbooks (and their skills) you have access to |
| `recipes_recall` | Recall the full content of a previously installed skill |
| `recipes_recipify` | Classify + validate a skill before publishing |
| `recipes_carousel_today` | Get today's editorially curated skill picks |
| `recipes_doctor` | Diagnose issues with installed skills |
| `recipes_seeker` | Find related skills and dependency edges |
| `recipes_subrecipe_resolve` | Resolve nested skill dependencies |
| `recipes_sync` | Auto-update installed skills (APPLY / DRY_RUN) |

---

## Pricing

| Tier | Price | What you get |
|------|-------|-------------|
| **Free** | €0/mo | Search, install free-tier skills, 5 installs |
| **Pro** | €20/mo | Unlimited installs, Pro-tier skills, cookbook sharing |
| **Pro+** | €100/mo | Everything in Pro + private cookbooks, priority support, analytics |

All tiers include MCP access. Publishers earn on every attributed use.

---

## What's New in v7.1

- **Cookbook share tokens** — share a cookbook with any agent via a single `cbt_` token
- **Auto-update via `recipes_sync`** — keep installed skills current with zero effort
- **StreamableHTTP MCP** — cleaner transport, better error handling, no SSE fallback needed
- **BM25 reindex on publish** — new skills are searchable within seconds

---

## Links

- 🌐 [recipes.wisechef.ai](https://recipes.wisechef.ai) — browse the marketplace
- 📖 [API docs](https://recipes.wisechef.ai/docs/api-reference) — full REST reference
- 🐛 [Issues](https://github.com/wisechef-ai/recipes-skill/issues) — report bugs
- 💬 [Discord](https://discord.gg/wisechef) — community support

---

*Recipes is built by [WiseChef](https://wisechef.ai). Licensed under Apache 2.0.*
