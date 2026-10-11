# Quickstart: Install & Update Your First Skill in 5 Minutes

This guide shows you how to install a skill from the LoopSkill marketplace and keep it up to date.

## Prerequisites

- An API key from [app.loopskill.io](https://app.loopskill.io) (free tier works)
- An MCP-connected agent (Hermes, Claude Desktop, Codex CLI, etc.)

## Step 1: Search for a Skill

Use `loopskill_search` to find skills relevant to your use case:

```
> loopskill_search({"query": "SEO audit for marketing agencies"})
```

You'll get ranked results with descriptions, ratings, and install counts.

## Step 2: Install the Skill

Use `loopskill_install` to add the skill to your workspace:

```
> loopskill_install({"skill_slug": "seo-audit-pro"})
```

This downloads the skill files to your `skills/` directory and makes them immediately available to your agent.

## Step 3: Check Your Bundle Status

Use `loopskill_list_bundle` to see all installed skills and their update status:

```
> loopskill_list_bundle({})
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

## Step 4: Update when a new version is published

Updating is two moves: move the bundle's pins on the server, then refresh the files on each machine.

```
> loopskill_sync({"bundle_id": "<your bundle id>", "dry_run": true})    # preview the pin changes
> loopskill_sync({"bundle_id": "<your bundle id>"})                     # apply them
> loopskill_bundle_install({"bundle_id": "<your bundle id>"})           # on EACH machine
```

`loopskill_sync` only changes the bundle's version pins on the server — it never touches files on your machine. `loopskill_bundle_install` returns a download URL and sha256 for every skill; download each tarball, check its sha256, and unpack it into your skills folder. Re-running the install is the update.

## Step 5: Recall a Skill Anytime

Need the full content of a previously installed skill?

```
> loopskill_recall({"skill_slug": "seo-audit-pro"})
```

Returns the complete skill body — no re-download needed.

## The update loop

Here's the recommended pattern for production agents:

1. **On startup**: Call `loopskill_list_bundle` to check current state
2. **Periodically** (hourly/daily): `loopskill_sync` the bundle, then re-run `loopskill_bundle_install` on each machine and replace any skill whose sha256 changed
3. **On demand**: Use `loopskill_recall` to refresh any skill's instructions

---

**That's it.** The same bundle, verified by sha256, on every machine you run.
