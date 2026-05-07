# Quickstart: Install & Auto-Update Your First Skill in 5 Minutes

This guide shows you how to subscribe to a skill from the Recipes marketplace and keep it automatically updated.

## Prerequisites

- An API key from [recipes.wisechef.ai](https://recipes.wisechef.ai) (free tier works)
- An MCP-connected agent (Hermes, Claude Desktop, Codex CLI, etc.)

## Step 1: Search for a Skill

Use `recipes_search` to find skills relevant to your use case:

```
> recipes_search({"query": "SEO audit for marketing agencies"})
```

You'll get ranked results with descriptions, ratings, and install counts.

## Step 2: Install the Skill

Use `recipes_install` to add the skill to your workspace:

```
> recipes_install({"skill_slug": "seo-audit-pro"})
```

This downloads the skill files to your `skills/` directory and makes them immediately available to your agent.

## Step 3: Check Your Cookbook Status

Use `recipes_list_cookbook` to see all installed skills and their update status:

```
> recipes_list_cookbook({})
```

The response includes a `cookbook_status` block for each skill:

```json
{
  "cookbook_id": "abc-123",
  "skills": [
    {
      "slug": "seo-audit-pro",
      "version": "1.2.0",
      "status": "current",
      "latest_version": "1.2.0"
    }
  ]
}
```

## Step 4: Auto-Update with recipes_sync

When a publisher releases a new version, `recipes_sync` keeps you current:

```
> recipes_sync({"mode": "APPLY"})
```

This:
1. Checks all installed skills for updates
2. Downloads and applies new versions
3. Returns a summary of what changed

Want to preview first? Use `DRY_RUN` mode:

```
> recipes_sync({"mode": "DRY_RUN"})
```

This shows what *would* change without modifying anything.

## Step 5: Recall a Skill Anytime

Need the full content of a previously installed skill?

```
> recipes_recall({"skill_slug": "seo-audit-pro"})
```

Returns the complete skill body — no re-download needed.

## The Auto-Update Loop

Here's the recommended pattern for production agents:

1. **On startup**: Call `recipes_list_cookbook` to check current state
2. **Periodically** (hourly/daily): Call `recipes_sync({"mode": "APPLY"})` to pull updates
3. **On demand**: Use `recipes_recall` to refresh any skill's instructions

---

**That's it.** Your agent now has a self-updating skill library. No manual config. No drift. 🎯
