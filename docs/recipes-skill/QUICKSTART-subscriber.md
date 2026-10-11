# Quickstart: Install & Update Your First Skills in 5 Minutes

This guide shows you how to install skills from the LoopSkill marketplace, keep them in one bundle, and update them on every machine you use. Every call below was run end to end on two machines (Debian x86_64 and macOS arm64) on 2026-10-11.

## Prerequisites

- An API key: sign in at [app.loopskill.io](https://app.loopskill.io/signin), then copy the key from your Library or create one on your Account page (the free tier works). Free catalog skills also install with no key at all.
- An MCP-connected agent pointed at `https://app.loopskill.io/api/mcp/http/` with the header `x-api-key: <your key>`.

## Step 1: Search for a skill

```
> loopskill_search({"query": "code review"})
```

Each result carries a `slug` — that is the identifier the next steps use.

## Step 2: Install one skill

```
> loopskill_install({"slug": "code-review"})
```

The response is a download envelope: `tarball_url`, `checksum_sha256`, `version`. Download the tarball, check that the sha256 of the bytes matches `checksum_sha256`, and unpack it into your agent's skills folder (for Claude Code: `~/.claude/skills/<slug>/`).

## Step 3: Put your skills in a bundle (once)

A bundle is a named list of skills you install together. Create it once, from any machine:

```
> loopskill_compose_bundle_from_links({"links": ["skill://code-review", "skill://pr-draft"], "name": "client-a"})
```

Keep the returned `bundle` id. To add a skill later: `POST /api/bundles/<bundle id>/skills` with `{"slug": "<slug>"}` (there is no MCP tool for this yet).

On the Free tier the bundle is private and counts toward your private-bundle limit; your Liked list counts too.

## Step 4: Install the bundle on every machine

```
> loopskill_bundle_install({"bundle_id": "<bundle id>"})
```

It returns one download envelope per skill. Do the same download → sha256 check → unpack for each, and keep a small lockfile of slug, version and sha256 so the next run can skip unchanged skills.

## Step 5: Update

```
> loopskill_sync({"bundle_id": "<bundle id>", "dry_run": true})   # preview the pin changes
> loopskill_sync({"bundle_id": "<bundle id>"})                    # move the pins to the latest versions
> loopskill_bundle_install({"bundle_id": "<bundle id>"})          # then, on EACH machine
```

`loopskill_sync` only changes the bundle's version pins on the server — it never touches files on your machine. Re-running `loopskill_bundle_install` and replacing any skill whose sha256 changed is what updates a machine.

## Step 6: Find a skill by what it does

```
> loopskill_recall({"query": "summarise a pull request"})
```

`loopskill_recall` is ranked search (BM25 + vector) over the catalog; use `loopskill_install` to fetch a result.

---

**That's it.** One bundle, the same sha256 on every machine you run.
