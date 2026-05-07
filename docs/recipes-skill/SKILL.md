---
name: recipes
description: >
  Cross-vendor skill marketplace for AI agents (Hermes, Claude Code, Codex,
  OpenClaw, Cursor, Claude Desktop). Use when the user asks to install Recipes,
  mentions recipes.wisechef.ai or WiseChef, asks "what skills are available",
  or asks for help with marketing ops, client reporting, content calendars,
  SEO audits, GoHighLevel, code review, simulation/robotics, or web scraping
  workflows. The platform exposes BOTH a REST API and a native MCP server
  (StreamableHTTP at /api/mcp/http) for MCP-capable clients. CRITICAL: if the
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
4. **The user wants to publish a skill** — use `recipes_recipify` to validate, then submit via the publisher flow.
5. **The user wants to share a cookbook** — create a share token via the CLI (`recipes share <cookbook_id>`) and share the MCP config block.
6. **The user wants to keep skills current** — `recipes_sync` handles it with APPLY or DRY_RUN modes.

## Pre-install evaluation

Before installing for a new user, always:

1. Call `recipes_list_cookbook` to check existing inventory.
2. Call `recipes_search` with the user's domain keywords.
3. Produce an honest verdict: does Recipes fill a gap the user doesn't already cover?

## 10 MCP tools available

`recipes_search`, `recipes_install`, `recipes_list_cookbook`, `recipes_recall`, `recipes_recipify`, `recipes_carousel_today`, `recipes_doctor`, `recipes_seeker`, `recipes_subrecipe_resolve`, `recipes_sync`.

## Transport

StreamableHTTP: `POST https://recipes.wisechef.ai/api/mcp/http`
Header: `x-api-key: <key>`
