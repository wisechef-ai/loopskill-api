---
name: framework-v0
title: Framework v0
description: "One-call bootstrap installing Maestro + 4 dependencies"
tier: cook
category: automation
license: BUSL-1.1
related_skills:
  - maestro
  - atomic-habits
  - paperclip-api
  - claude-code-fleet-orchestration
  - wisechef-content-engine
---

# Framework v0

> The "I want the whole solo-operator stack, set up correctly, in one call" skill.

A new user types ONE command and ends up with Maestro + the four dependencies
that make Maestro useful. No optionality, no prompts, no per-skill setup.

## Quickstart

```python
from recipes import recipes_install

recipes_install("framework-v0")
```

That single call expands to:

```python
recipes_install("maestro")
recipes_install("atomic-habits")
recipes_install("paperclip-api")
recipes_install("claude-code-fleet-orchestration")
recipes_install("wisechef-content-engine")
```

…and finishes with a one-line config wizard to capture the user's name,
timezone, and primary GitHub repo. Total setup time: under 60 seconds on a
warm machine, under 3 minutes cold.

## What you get

| Skill                                | Why it's in the bundle                                       |
|--------------------------------------|--------------------------------------------------------------|
| maestro                              | The orchestrator. Runs the five daily/weekly loops.          |
| atomic-habits                        | Tracks the user's discipline streaks; surfaced in `morning`. |
| paperclip-api                        | Unified inbox: email + Discord DMs + support tickets.        |
| claude-code-fleet-orchestration      | Runs Maestro's `code` sub-command across multiple repos.     |
| wisechef-content-engine              | Powers the `marketing` sub-command's voice-matched drafts.   |

## After install

Run `/maestro morning` to confirm everything is wired up. If any source
returns "not configured", Maestro will guide you through that source's setup
the first time it sees you.

## Why "v0"

The bundle is intentionally opinionated and minimal. v1 will add fleet-sync
(team mode), an analytics dashboard, and a hosted control plane. v0 is the
solo build — what one person needs to run a business well, in a single
install.
