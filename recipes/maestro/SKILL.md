---
name: maestro
title: Maestro
description: "Solo-operator AI agent that runs your business while you sleep — morning briefing, marketing, code, tickets, weekly review."
tier: pro
category: automation
license: BUSL-1.1
related_skills:
  - framework-v0
  - atomic-habits
  - paperclip-api
  - claude-code-fleet-orchestration
  - wisechef-content-engine
---

# Maestro

> The solo-operator's chief of staff. Runs the five repeating loops of a one-person business so the human can stay on craft.

Maestro is the renamed successor to the `chef` skill. Same orchestration model,
sharper scope: it is the **single entry point** the user invokes by hand each
day, and everything else is delegated through sub-commands.

## Daily mental model

```
06:00  /maestro morning      → 1-screen briefing
09:00  /maestro code         → ticket-driven coding session
13:00  /maestro marketing    → outbound + content
17:00  /maestro tickets      → support inbox triage
Friday /maestro weekly       → KPI review + next-week plan
```

Maestro does not replace the human. Each sub-command produces a draft + a
review checkpoint. The human approves, edits, or bins. Maestro learns from
that signal.

## Sub-commands

### `morning` — 1-screen briefing

Pulls overnight signals from every connected source and compresses them into
≤ 600 tokens of "what changed and what needs your attention today."

Sources:
- GitHub: PR review requests, failing CI on `main`, new issues tagged `needs-triage`
- Stripe: yesterday's revenue + churn deltas vs. trailing 7d
- Discord/Slack: unread `@mentions` in monitored channels
- Calendar: today's meetings + prep notes
- Atomic Habits skill: streak status for the user's daily disciplines

Output: a Markdown briefing with three sections — **Wins**, **Watchouts**,
**First move of the day**.

### `marketing` — outbound + content

Runs the wisechef-content-engine pipeline with the user's voice profile
applied. Produces:
- 1 LinkedIn post draft (≤ 280 chars hook + 800-char body)
- 1 X thread draft (5-7 posts)
- 3 cold-outbound replies in the user's tone (one per high-priority lead in
  the paperclip-api inbox)

Each draft is delivered as a unified diff against `content/drafts/<date>.md`
so the user can `git add -p` what they want to keep.

### `code` — ticket-driven coding session

Selects the next ticket from the user's queue (Linear, GitHub Issues, or a
flat `TODO.md` — whichever is configured). Runs through:

1. Read ticket + recent related commits
2. Sketch a plan (≤ 5 bullets)
3. Wait for user `y/n` on the plan
4. Implement on a fresh branch
5. Run the project's test suite
6. Open a PR with a structured description

If step 5 fails, Maestro stops and surfaces the failure — it never pushes red.

### `tickets` — support inbox triage

Walks the support inbox (configured via `paperclip-api`), classifies each new
ticket into:

- **bug** → opens a GitHub issue with repro steps extracted from the ticket
- **billing** → drafts a reply + flags the user for sign-off
- **feature** → adds to `feedback/feature-requests.md` with sender
- **noise** → archives with a one-line reason

The user reviews the classification batch in ≤ 3 minutes and approves.

### `weekly` — KPI review + next-week plan

Runs every Friday afternoon (or on-demand). Produces:
- Last week's KPIs vs. targets (revenue, MAU, NPS, code merged, tickets closed)
- A 3-bullet "what worked / what didn't / what to change"
- Next week's top-3 outcomes, derived from the user's quarterly OKRs

Logged to `weekly-reviews/YYYY-WW.md` and committed to the user's journal repo.

## Configuration

Maestro reads `~/.maestro/config.toml`:

```toml
[user]
name = "Adam"
timezone = "Europe/Warsaw"

[sources.github]
repos = ["wiseflow-os/wisechef", "wiseflow-os/recipes"]

[sources.stripe]
account_id = "acct_xxx"

[sources.linear]
team_key = "ENG"

[voice]
profile_path = "~/.maestro/voice/adam.json"
```

The first run guides the user through each section interactively.

## What Maestro does NOT do

- It does not auto-merge code, auto-send emails, or auto-publish content.
  Every external-side-effect requires explicit human approval.
- It does not store credentials. All secrets live in the user's password
  manager and are read via OS keychain integration.
- It does not call out to any third-party LLM other than the one the user
  has configured (default: Claude via Anthropic SDK).

## Migration from `chef`

If you were using the `chef` skill: there is nothing to do. The `chef` slug
will redirect to `maestro` on the marketplace for 90 days. After that window
closes, customer-side shims at `~/.hermes/skills/chef/` should be removed
manually (Adam has a one-liner queued for the next fleet ping).
