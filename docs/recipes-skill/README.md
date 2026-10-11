# LoopSkill — The Skill Marketplace for AI Agents

**Give your agent superpowers. Search, install, and run curated skills — in under 60 seconds.**

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

---

## The 60-Second Pitch

You're an AI agent (or you fleet-own one). You want **composable, trusted capabilities** — not brittle API glue. LoopSkill is the marketplace where human-reviewed skills live. One MCP connection gives your agent 10+ tools for search, install, recall, diagnostics, and more. No dependencies. No vendor lock-in. Just skills that work.

**Publishers** earn recurring revenue via usage-attributed Stripe Connect payouts. **Subscribers** get auto-updating skills with zero config drift. **Teams** share private bundles with a single CLI command.

---

## Quick Install

### Hermes (StreamableHTTP)

Add to `~/.hermes/config.yaml`:

```yaml
mcpServers:
  loopskill:
    transport: streamable-http
    url: https://app.loopskill.io/api/mcp/http
    headers:
      x-api-key: <key>
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "loopskill": {
      "type": "streamable-http",
      "url": "https://app.loopskill.io/api/mcp/http",
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
export LOOPSKILL_API_KEY=YOUR_API_KEY
# Then reference in your Codex MCP config pointing to:
# https://app.loopskill.io/api/mcp/http
```

> Get your API key at [app.loopskill.io/signin](https://app.loopskill.io/signin) — free tier available.

---

## Quickstarts

| Guide | Time | What you'll do |
|-------|------|----------------|
| [Publisher quickstart](./QUICKSTART-publisher.md) | 5 min | Publish your first skill to the marketplace |
| [Subscriber quickstart](./QUICKSTART-subscriber.md) | 5 min | Install + update your first skill |
| [Bundle sharing](./QUICKSTART-share.md) | 3 min | Share a private bundle with another agent |

---

## MCP tools

Once connected, your agent gets these tools (a selection — `tools/list` on the MCP endpoint returns the full set):

| Tool | What it does |
|------|-------------|
| `loopskill_search` | BM25 + semantic search across all marketplace skills |
| `loopskill_install` | Return a signed download URL + sha256 for one skill |
| `loopskill_compose_bundle_from_links` | Create a new private bundle from skill / bundle links |
| `loopskill_bundle_install` | Return download URLs + sha256 for every skill in a bundle |
| `loopskill_list_bundle` | List a bundle and its skills |
| `loopskill_recall` | Recall the full content of a previously installed skill |
| `loopskill_doctor` | Diagnose issues with installed skills |
| `loopskill_seeker` | Find related skills and dependency edges |
| `loopskill_subskill_resolve` | Resolve nested skill dependencies |
| `loopskill_sync` | Move a bundle's server-side version pins to the latest published versions (APPLY / DRY_RUN). It does not change files on your machine: re-run `loopskill_bundle_install` on each machine to update them. |

---

## Pricing

| Tier | Price | What you get |
|------|-------|-------------|
| **Free** | $0 | Install every public skill and bundle, unlimited installs, 2 private bundles, 1 API key |
| **Pro** | $9.95/mo | Everything in Free, 50 private bundles, 10 API keys |

Two capacity limits differ: private bundles (Free 2, Pro 50) and API keys (Free 1, Pro 10). Your Liked list is a private bundle and counts as one from the moment you open your Library, so Free leaves room for one more. One API key works on every machine at once; separate keys let you revoke one machine or client without touching the rest. Public bundles are unlimited on every tier.

Pro also unlocks the hosted authoring and deploy tools: forking a skill into your own editable copy, turning a draft into a skill, managed bundle deploys with rollback, and a live sync subscription. Every catalog skill is free on both tiers, and re-running the install to update works the same on both.

You may not need Pro at all: public bundles are free and unlimited, self-hosting the whole platform is free (MPL-2.0), and a private git repo with symlinks also keeps skills in sync across machines at no cost.

Free skills also install with no account at all. Canonical, current pricing: https://app.loopskill.io/pricing

---

## What's New in v7.1

- **Bundle share tokens** — share a bundle with any agent via a single `cbt_` token
- **`loopskill_sync`** — moves a bundle's version pins to the latest releases; re-run `loopskill_bundle_install` on each machine to update its files
- **StreamableHTTP MCP** — cleaner transport, better error handling, no SSE fallback needed
- **BM25 reindex on publish** — new skills are searchable within seconds

---

## Links

- 🌐 [app.loopskill.io](https://app.loopskill.io) — browse the marketplace
- 📖 [API docs](https://app.loopskill.io/docs/api-reference) — full REST reference
- 🐛 [Issues](https://github.com/wisechef-ai/loopskill-skill/issues) — report bugs
- 💬 [Discord](https://discord.gg/wisechef) — community support

---

*LoopSkill is built by [WiseChef](https://wisechef.ai). Licensed under Apache 2.0.*
