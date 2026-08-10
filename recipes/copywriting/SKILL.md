---
name: copywriting
description: >
  Write and edit marketing copy that survives scrutiny — landing pages, emails,
  ads, product descriptions, and launch posts. Applies the classic direct-response
  discipline (one reader, one promise, one action) plus a claim-grounding pass
  that refuses to ship any assertion the source material cannot support. Use when
  asked to write, rewrite, tighten, or critique copy, or when a draft reads
  generic, hypey, or interchangeable with a competitor's. Includes named
  frameworks (PAS, AIDA, the 4 Us), a specificity ladder, and a pre-ship checklist.
tier: free
category: marketing
license: MIT
tags: [copywriting, marketing, landing-page, email, positioning, editing]
related_skills: [hundred-million-offers, humanizer, obviously-awesome]
os_supported: [linux, macos, windows]
---

# Copywriting

Copy is not decoration. It is the argument that makes a reader act. This skill
covers writing it and, more often, fixing it.

## When to use

- Writing a landing page, email, ad, product description, or launch post
- Rewriting copy that reads generic, hypey, or interchangeable with a competitor's
- Critiquing a draft before it ships
- Turning a feature list into something a stranger cares about

## NOT for

- Long-form editorial or documentation — different craft, different rules
- Removing AI tells from existing prose — use `humanizer`
- Deciding *what* to sell or at what price — use `hundred-million-offers`
- Choosing a market position — use `obviously-awesome`

## Method

### Step 1 — Answer four questions before writing a word

Copy fails at the brief, not the sentence. Write these down:

1. **Who exactly is reading this?** Not "developers" — *"a solo consultant who
   just lost an afternoon to a broken deploy."*
2. **What do they already believe?** You are joining a conversation already
   happening in their head, not starting one.
3. **What is the ONE action?** One page, one action. A second call-to-action
   competes with the first, it does not add to it.
4. **What is the single most persuasive true thing you can say?** Lead with it.

If you cannot answer #1 and #4, no amount of wordcraft will save the draft.

### Step 2 — Pick a structure

**PAS — Problem, Agitate, Solution.** Best for a pain the reader already feels.

```
Problem:   Name the pain in their words.
Agitate:   The cost of it continuing. Concrete, not catastrophising.
Solution:  Your thing, as the obvious relief.
```

**AIDA — Attention, Interest, Desire, Action.** Best for cold traffic.

**The inverted pyramid.** Best for readers who will not scroll: conclusion first,
support after. Most product pages should use this and do not.

### Step 3 — Write the headline last, and write ten

The headline does 80% of the work. Never keep the first one. Test each against
**the 4 Us**: Useful, Urgent, Unique, Ultra-specific — a strong headline hits at
least two.

### Step 4 — Climb the specificity ladder

Vague copy is the default failure. Push every claim down this ladder:

| Rung | Example |
|---|---|
| Generic | "Save time on deploys" |
| Better | "Cut deploy time" |
| Better | "Cut deploy time by half" |
| **Specific** | **"Deploys went from 40 minutes to 6."** |

**Never fabricate a number to climb the ladder.** If the specific version is not
supported by the source material, stay on the vague rung and flag it — see the
gate in step 6.

### Step 5 — Cut

The first draft is always long. In order:

1. Delete the first paragraph. It is usually throat-clearing.
2. Delete every adverb, then restore only the load-bearing ones.
3. Replace every "we" with "you" where the sentence still works.
4. Kill hedges: *very, really, quite, just, actually, basically, simply*.
5. Read it aloud. Anywhere you stumble, the reader stumbles harder.

**Ban list** — these signal generic AI-adjacent marketing copy:
`revolutionary` · `seamless` · `game-changing` · `unlock` · `leverage` ·
`elevate` · `robust` · `cutting-edge` · `in today's fast-paced world` ·
`we're excited to announce`

### Step 6 — The claim gate (do not skip)

For **every** factual assertion in the draft, one of these must be true:

- it is traceable to the source material you were given, **or**
- it is marked as an assumption for the human to confirm, **or**
- it is cut

A number you cannot source is not a persuasive detail — it is a liability that
destroys trust the moment one reader checks it. An unsourced claim takes the
weaker true form or it does not ship.

## Pre-ship checklist

- [ ] One reader, one promise, one action
- [ ] Headline earns the next line; first line earns the second
- [ ] Every claim is sourced, flagged, or cut
- [ ] Specific beats vague everywhere the source allows
- [ ] Zero ban-list words
- [ ] Reads cleanly aloud
- [ ] The call to action names what happens next ("Start the 6-minute setup",
      not "Learn more")

## Worked example

**Before** (generic, unsourced, two actions):

> Our revolutionary platform leverages cutting-edge AI to seamlessly streamline
> your workflow. Sign up today or learn more about our features!

**After** (one action, specific, sourced):

> Your deploy takes 40 minutes. Ours takes 6.
>
> Same pipeline, same tests — we just stopped rebuilding what did not change.
>
> **Start the 6-minute setup →**

What changed: the ban-list words are gone, the claim is concrete and traceable,
"our platform" became the reader's problem, and the second call-to-action was
deleted rather than demoted.

## Related

- `hundred-million-offers` — decide *what* to sell before writing about it
- `humanizer` — strip AI tells from a finished draft
- `obviously-awesome` — position the product before you describe it
