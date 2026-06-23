---
name: aitoearn
description: >
  Multi-platform content marketing agent (Douyin, Xiaohongshu, Kuaishou, Bilibili,
  WeChat Channels, TikTok, YouTube, Facebook, Instagram, Threads, X, Pinterest,
  LinkedIn). Thin MCP-first wrapper around the open-source AiToEarn project —
  publishes, engages, and tracks monetization (CPS / CPE / CPM payouts) across
  the channels above from one MCP server. Use when the team ships content across
  ≥3 platforms and wants one tool to publish/engage/monetize instead of N
  per-platform recipes. ICP: 1-person AI owners and 1-7 person builder teams
  running multi-platform content for clients.
tier: pro
category: marketing
license: MIT
tags: [content, multi-platform, marketing, monetization, mcp, social-publishing]
related_skills: [plan-for-goal]
os_supported: [linux, macos, windows]
---

# AiToEarn — multi-platform content + monetization wrapper

> Thin wrapper recipe. Heavy lifting lives upstream (the AiToEarn open-source
> project). This recipe documents the install path, the auth model, and the
> pitfalls so you can wire it into any MCP-capable agent in under five minutes.

## When to use

- Posting the same content (or platform-tailored variants) across 3+ social channels and you don't want to maintain N per-platform integrations
- Running a content marketing pipeline for clients and you want a single MCP surface for publish / engage / monetize
- Need built-in CPS / CPE / CPM monetization (brand-task fulfillment with measured payouts) rather than gluing a custom analytics layer on top of generic publish tools
- Operating bilingual / CN+INTL distribution (Douyin/Xiaohongshu/Kuaishou + TikTok/YouTube/IG/X all in one tool)

## NOT for

- Single-platform workflows — a dedicated platform skill is leaner
- Pure analytics / no publishing — the value is the unified write path
- High-volume bulk scheduling (>500 posts/day) — upstream is built for agent-paced workflows, not enterprise schedulers
- Workflows that require deep platform-specific features (e.g. TikTok Live, X Spaces) — upstream MCP exposes the common-denominator surface

## Supported channels

| Region | Platforms |
|---|---|
| CN | Douyin (抖音), Xiaohongshu (小红书), Kuaishou (快手), Bilibili (B站), WeChat Channels (微信视频号) |
| INTL | TikTok, YouTube, Facebook, Instagram, Threads, X (Twitter), Pinterest, LinkedIn |

## Install — three paths (in order of preference)

### 1. MCP-first (recommended)

Add the hosted MCP server to your agent's MCP config. For Claude Desktop, edit `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "aitoearn": {
      "url": "https://aitoearn.ai/api/unified/mcp",
      "headers": {
        "Authorization": "Bearer <YOUR_AITOEARN_API_KEY>"
      }
    }
  }
}
```

Restart the agent. The aitoearn MCP tools (`publish`, `engage`, `analytics`, `monetize`) appear in the tool list. No local install, no Node/Electron build, no Docker.

For other MCP-capable agents (Cursor, OpenClaw, Hermes, custom clients), the same URL + auth header drops into their respective MCP config files.

### 2. OpenClaw plugin

If your stack runs on OpenClaw / Claude Code / similar plugin-based agent harnesses:

```bash
npx -y @aitoearn/openclaw-plugin-cli install
```

The plugin registers the same tool surface as the MCP path but routes through the upstream plugin namespace.

### 3. Hosted web app (no agent)

For human-only workflows, sign in at `aitoearn.ai` (INTL) or `aitoearn.cn` (CN). Same dashboard, same channels — but the value of this recipe is the agent-driven path; the web app is the fallback.

## Acquiring an API key (3 steps)

1. Sign up at `aitoearn.ai` (INTL) or `aitoearn.cn` (CN). **Pick the region that matches where your content posts** — see the pitfall below.
2. Go to **Dashboard → API Keys → Create**. Copy the key.
3. Export the key as `AITOEARN_API_KEY` in your agent's env, or paste it into the MCP config's `Authorization` header.

Free tier exists. Paid tiers unlock higher API rate limits and broader channel access.

## Monetization model (the "earn" half)

AiToEarn exposes a `monetize` tool family that wires content publishing to brand-task payouts:

- **CPS** (cost-per-sale) — payout when content drives a tracked purchase
- **CPE** (cost-per-engagement) — payout per like / comment / share within attribution window
- **CPM** (cost-per-mille) — payout per 1000 impressions

The agent picks brand tasks, publishes content that fulfills them, and tracks payout state through the same MCP server. Treat this as opt-in: most teams start with `publish` + `engage` and adopt `monetize` after the content pipeline is stable.

## Pitfalls

1. **🚩 CN key on INTL endpoint (or vice versa) returns 401.** This is the single most common install failure. The two regions are separate tenants with separate API key databases. If the key was created on `aitoearn.cn`, it will NOT authenticate against `aitoearn.ai/api/unified/mcp`. Pick the region first, create the key second.

2. **Correct MCP path is `/api/unified/mcp`, NOT `/api/mcp`.** The shorter path returns 404. The "unified" segment is intentional — it's the merged server for both regions' tools.

3. **OpenClaw plugin and MCP server expose the same logical tools, but tool IDs differ.** If you migrate from one path to the other mid-pipeline, expect to rename tool calls in your prompts (`aitoearn_publish` vs `aitoearn__unified__publish` shapes).

4. **CN-channel publishing requires real-name verification on the dashboard side.** Douyin / WeChat Channels will accept the API call but the post lands in a moderation queue until the linked account is verified. INTL channels (TikTok, IG, X) follow the platform's own onboarding — most require an OAuth re-grant the first time the tool publishes.

5. **Rate limits are per-tier, per-channel.** A free-tier key publishing to all 13 channels in one minute will hit a 429 on the lowest-limit platform first. Stagger by 30-90s per channel or upgrade tier.

6. **Docker self-host is supported but unnecessary for most teams.** Upstream maintains the hosted MCP server with the same feature set as the self-host image. Self-host only if compliance requires on-prem.

7. **Source build (Node 20.18.x + Electron) is the most fragile path.** Use it only if you're actively contributing upstream. For consuming the tool, MCP is strictly better.

## Verification after install

```bash
# 1. List MCP tools (in your agent's tool inventory)
# Expect: aitoearn_publish, aitoearn_engage, aitoearn_analytics, aitoearn_monetize

# 2. Smoke test — fetch your account profile via the MCP tool
# (run via your agent; replaces the curl below)
# Tool: aitoearn_analytics
# Args: { "endpoint": "profile" }
# Expect: 200 with your account ID + tier label

# 3. Optional curl probe (proves the endpoint is up):
curl -sS -o /dev/null -w "HTTP %{http_code}\n" https://aitoearn.ai/api/unified/mcp
# Expect: HTTP 200 (the MCP handshake responds without auth on GET)
```

## Recipe shape

This is a wrapper recipe. The agent calls AiToEarn's MCP tools directly — there is no Python module to install, no local service to run, no cron to wire. The recipe's value is the install + auth + pitfalls knowledge above, plus the channel-selection guidance below.

### Channel-selection heuristic

When publishing the same campaign across channels, the agent should:

1. **Start with 2 channels per campaign**, not 13. Measure engagement before broadening — most teams publish to too many channels and see no signal anywhere.
2. **CN + INTL run on separate strategies.** Content that wins on Douyin rarely translates 1:1 to TikTok — even if the format is identical. Run separate campaigns per region.
3. **Pinterest and LinkedIn are evergreen / long-tail; the rest are short-tail.** Schedule them accordingly — Pinterest pins last weeks, X posts last hours.

## Upstream

- **Project:** https://github.com/yikart/AiToEarn
- **License:** MIT
- **Status:** Active development (last push within the past day at recipe authoring time)
- **Stars:** 13K+
- **Maintainer:** yikart (active)

## Related skills

- `plan-for-goal` — author the campaign plan-doc that drives the AiToEarn publish/engage loop.
