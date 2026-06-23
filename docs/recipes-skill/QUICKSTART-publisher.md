# Quickstart: Publish Your First Skill in 5 Minutes

This guide walks you through creating and publishing a skill to the Recipes marketplace using the MCP tools.

## Prerequisites

- An API key from [recipes.wisechef.ai](https://recipes.wisechef.ai) (free tier works)
- A GitHub account (for repo hosting)
- An MCP-connected agent (Hermes, Claude Desktop, Codex CLI, etc.)

## Step 1: Validate Your Skill

Use `recipes_recipify` to classify and validate your skill before submitting:

```
> recipes_recipify({"name": "my-awesome-skill", "description": "Does X for Y audience"})
```

This returns a quality score and flags any issues.

## Step 2: Create a GitHub Repo

```bash
mkdir my-awesome-skill && cd my-awesome-skill
```

Create a `SKILL.md` at the repo root:

```yaml
---
name: my-awesome-skill
description: >
  One-line description of what your skill does and who it's for.
tags: [automation, marketing]
requiredEnv: []
permissions:
  - network: "Explain what network access is for"
---
```

Below the frontmatter, write instructions for the agent on when and how to use the skill.

## Step 3: Install Locally (Dry Run)

Use `recipes_install` to test the skill in your workspace:

```
> recipes_install({"skill_slug": "my-awesome-skill", "dry_run": true})
```

Verify the output looks correct.

## Step 4: Publish via API

Use `curl` to submit your skill:

```bash
curl -X POST https://recipes.wisechef.ai/api/skills/_publish \
  -H "Content-Type: application/json" \
  -H "x-api-key: YOUR_API_KEY" \
  -d '{
    "repo_url": "https://github.com/your-username/my-awesome-skill",
    "tier": "free"
  }'
```

Tiers: `free` (public catalog), `pro` (€20/mo paywalled), `pro_plus` (€100/mo paywalled).

## Step 5: Verify It's Live

```bash
curl https://recipes.wisechef.ai/api/skills/my-awesome-skill \
  -H "x-api-key: YOUR_API_KEY"
```

You should see your skill metadata returned. The editorial review typically completes within 24 hours.

## Step 6: Earn

Once approved, every attributed use of your skill generates revenue. Track earnings at [recipes.wisechef.ai/dashboard](https://recipes.wisechef.ai/dashboard).

---

**That's it.** From idea to published skill in under 5 minutes. 🚀
