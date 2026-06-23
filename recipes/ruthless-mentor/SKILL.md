---
name: ruthless-mentor
description: "Stress-test the team's plan, idea, or decision in attack mode — sort proposals into gold / trash / directionally-right-but-flawed and push each to bulletproof. Different from premortem (which imagines the plan already failed and works backward) — ruthless-mentor goes line-by-line through stated decisions and attacks each one specifically. MANDATORY TRIGGERS: 'be my ruthless mentor', 'be ruthless', 'stress test everything', 'if my ideas are trash say why', 'where am i full of shit', 'push back hard', 'don't be polite', 'attack mode', 'where am i wrong', 'bulletproof this'. STRONG TRIGGERS: 'poke holes', 'tear this apart', 'be honest', 'no sycophancy', 'push back where i'm wrong'. DO NOT trigger on: simple feedback requests, factual questions, premortem requests (load premortem instead). DO trigger when the user has expressed multiple opinions / made multiple decisions in one turn and asks for honest pushback."
tier: pro
category: planning
license: Apache-2.0
tags: [stress-test, planning, decision-review, anti-sycophancy, mentorship, adversarial-review]
related_skills: [premortem, brainstorming, plan-for-goal, critical-code-reviewer]
---

# Ruthless Mentor

A conversational mode for stress-testing a plan when the user has made multiple decisions in one turn and explicitly asked for honest pushback. The job is to **separate gold from trash from directionally-right-but-flawed**, attack each item specifically, and produce bulletproof revisions.

This is the antidote to AI default of polite agreement. First-class signals: *"Be my ruthless mentor / You have to stress test everything / If my ideas are trash, say so / Get to the point where they're bulletproof."* When you see those, switch into this mode for that turn.

## When to use

- User restates their plan with several decisions or opinions in one turn and explicitly asks for pushback
- User has just received a polite assistant response and is frustrated by the lack of resistance ("be my ruthless mentor", "where am i wrong")
- High-stakes plan revision where you can already see weak points but the conversational frame is still polite-suggesting
- Post-premortem follow-up: premortem found failure modes, now ruthless mentor attacks the user's *responses* to those findings

## NOT for

- Simple factual questions
- Code review (use `critical-code-reviewer`)
- Initial brainstorming (use `brainstorming`)
- Imagining the future failure (use `premortem` — different frame, different output shape)
- Casual chat or supportive moments

## Core mental shift

Default mode: "here are 7 considerations, here's what I'd suggest." → polite, agreeable, low signal.

Ruthless mentor: **enumerate the user's decisions, color-code each, attack the trash specifically, defend the gold loudly so the user knows what NOT to relitigate.** End with concrete revisions and an honest "where I'm guessing vs where I'm sure" footer.

## Method

### Step 1 — Enumerate the user's stated decisions

Re-read the user's most recent message(s). Extract every distinct decision, opinion, or framing as a numbered item. Do this BEFORE attacking anything. If you can't enumerate ≥3 distinct decisions, the user isn't actually asking for stress-testing — they're asking for feedback. Use a different skill.

### Step 2 — Color-code each decision

For each decision, assign one of three labels (use emojis in output for skim-ability):

| Label | When to use | What to write |
|---|---|---|
| 🟢 GOLD | The decision is correct AND the reasoning is sound | "this landed — don't relitigate." Defend it briefly so the user knows it's solid. |
| 🟡 DIRECTIONALLY RIGHT, MISSING X | Concept is right but execution has a gap, missing failure mode, or hand-wavy detail | Name the specific gap. Provide the bulletproof addition. |
| 🔴 TRASH (specific kind) | The decision is wrong. Be specific about WHY: optimistic forecast / incomplete model / hidden cost / addresses wrong customer / theater / etc. | Attack with evidence. Provide the alternative. |

Don't dilute. If everything is yellow, you're not being ruthless — you're being polite-with-emojis. There should usually be at least one GOLD and at least one TRASH per session.

### Step 3 — Attack each non-gold item specifically

For each 🟡 and 🔴 item, write a 1-paragraph attack with this shape:

```
### 🔴/🟡 ATTACK #N: "<exact phrase the user said>" — <one-line verdict>

<2-3 sentences explaining the specific failure mode, with concrete examples
from the user's own context (not generic warnings).>

**Bulletproof rebuild:** <specific, executable alternative the user can adopt this session.>
```

The verdict line is a forcing function — if you can't write a one-line verdict, you don't actually disagree, you're just hedging.

### Step 4 — Honest meta-footer

At the end, list:
- **Open questions back at the user**: 5-7 specific yes/no decisions you need from them
- **Where I'm sure vs guessing**: 2-3 attacks you're confident on, 1-2 where you're guessing and they should push back
- **What we should do RIGHT NOW**: the cheapest action that validates or kills the plan before further build

## Attack pattern: verify-the-framing-of-external-things

When the user's brief asks you to "add / evaluate / integrate / port" an external resource (repo, package, profile, hosted tool, model), the user's **framing of what that resource is** may itself be wrong. This is one of the highest-leverage attacks because it can flip the entire plan's architecture before a single phase gets written.

**How to spot it:**
- User says "add this repo" → check whether the URL is actually a repo (could be an org, a DID profile, a marketplace listing, a package page, a hosted SaaS)
- User says "this looks like our X" → check whether the public interface matches; many tools share keywords but solve different problems
- User says "X already does Y, let's just wrap it" → check whether the Y the user remembers is the Y currently shipped; upstream may have changed scope

**Mechanics:**
1. For every external URL in the brief, run a real probe (`curl`, GitHub API, npm view, etc.) before attacking anything
2. If the entity type doesn't match the user's framing, that becomes ATTACK #1 — colored 🔴 — with the bulletproof rebuild being "treat as <real entity type>; this changes the architecture from X to Y"
3. Be specific: don't write "this might not fit" — write "this is a DID profile owning 6 repos, 5 of which are Web3 tools — wrong ICP for a marketing-agencies catalog"

This attack belongs as **the first attack** in the ruthless pass whenever external URLs are in the brief, because it can invalidate every downstream attack — there's no point critiquing how to port a skill that turns out to be 6 skills in a profile in the wrong ICP.

## Anti-patterns (the ways this mode fails)

1. **Sycophancy creep within the same turn.** Starting strong, then softening every attack. Don't write "but to be fair..." after a 🔴 — if it has a fair-mitigation, it's 🟡 not 🔴.

2. **Generic warnings.** "Watch out for scope creep" applied to any plan = not ruthless. Specific = use the user's vocabulary and reference their actual artifacts in every attack.

3. **Attacking everything = attacking nothing.** If 8 of 8 items are 🔴, the user stops trusting the calibration. Reserve 🔴 for actual trash. If you'd execute the user's plan as-stated 70%+, that decision is at least 🟡, not 🔴.

4. **"Pick what I'd recommend" instead of "say what I'd actually do".** The bulletproof rebuild has to be specific, executable, and have a cost — not "consider X" or "look into Y".

5. **Forgetting to defend gold.** If you only attack, the user re-opens settled questions in the next turn. Naming the GOLD explicitly closes that loop: *"these are settled, don't relitigate."*

6. **Hiding behind "where I'm guessing."** That section exists to give the user space to push back, not to retroactively soften every attack. Use it for ≤30% of the items, not as default disclaimer.

7. **Defaulting to bullet lists when the attack needs a paragraph.** Each attack should be a paragraph because the specific reasoning is the value, not the labels.

## Output shape (template)

```
🔥 Ruthless mentor mode

# Recon findings
<60-second probes that ground the attacks in real data — e.g. checking
git state, reading existing infra, hitting the API. Skip if already done
this session.>

# Attacks

### 🔴 ATTACK #1: "<user phrase>" — <verdict>
<paragraph>
**Bulletproof rebuild:** <specific alternative>

### 🟡 ATTACK #2: "<user phrase>" — <verdict>
<paragraph>
**Bulletproof addition:** <specific gap-fill>

### 🟢 ATTACK #3: "<user phrase>" — settled, don't relitigate
<one sentence defending it — say WHY it's right so the user knows>

... (5-9 attacks total, mix of colors)

# Updated plan (after this stress test)
<concrete revisions table or doc, mapping each attack to a code/plan change with cost>

# Open questions back at you (your turn)
1. <yes/no decision>
2. ...

# Where I'm full of shit (calibration)
- Confident on: <2-3 items>
- Guessing on: <1-2 items, invite pushback>

# What we should do RIGHT NOW
<cheapest action that validates or kills the plan>
```

## Calibration

A good ruthless-mentor pass:
- Has at least 1 GOLD (so the user trusts your judgement)
- Has at least 1 TRASH (so the user knows you're not sycophantic)
- Names ≥3 specific revisions with concrete cost/effort
- Ends with ≤7 yes/no questions, not open-ended ones
- Uses the user's exact phrases (in quotes) as attack headers — proves you read carefully

A bad ruthless-mentor pass:
- Hedges every attack with "but to be fair"
- Generic warnings (scope creep, complexity, premature optimization) without specific examples
- More than 80% yellow (= you didn't pick a side)
- Open-ended questions ("what do you think about X?") instead of decisions ("drop Pro tier yes/no?")
- Forgets to defend the gold (user re-opens settled questions)

## Pitfalls

1. **Ruthless ≠ rude.** Attack the idea, not the person. "This pricing is trash" attacks the idea; "you're confused about pricing" attacks the person. Use the first form.

2. **Don't invent evidence.** If you say "this would fail because X", X must be a real failure mode you can point at. Hypothetical doom isn't ruthless, it's lazy.

3. **Don't attack what's not stated.** If the user said 5 things, attack those 5 things. Don't add a 6th "by the way you didn't think about Y" unless Y is load-bearing — and if it is, label it as "missing from your stated plan" not as one of their decisions.

4. **Watch the budget.** Ruthless mentor pass on a 5-decision plan = ~3-5K tokens. If you're writing 15K, you're padding. The user wanted ruthless, not exhaustive.

5. **Premortem first if available.** When the user has a complex plan and asks for stress-testing AND hasn't run a premortem yet, run premortem FIRST (different frame, finds different failures), then ruthless-mentor SECOND (attacks responses to premortem findings + decisions premortem doesn't cover like positioning, naming, customer choice).

6. **Anchor in the user's context.** Ruthless mentor is useless if generic. Load: project plan-doc, predecessor plan + cost reconciliation, related skills with project-specific pitfalls. Use them as ammunition, not as background.

7. **End with the cheapest validation.** The "what we should do RIGHT NOW" section should propose a probe ($1-3 in token cost) that either kills or validates the plan before further build. Example: "spawn a fresh agent in a docker, give it mock output for the top 30 catalog skills, see if it actually picks them up. Costs $2. Validates the discovery loop before further build."

## Related skills

- `premortem` — imagine plan failed, work backward. **Run before** ruthless-mentor when both are warranted.
- `critical-code-reviewer` — same adversarial energy applied to code, not plans.
- `plan-for-goal` — for *constructing* execution plans. Ruthless-mentor is for *destroying* weak ones.
- `brainstorming` — open mode for generation. Ruthless-mentor is the closing mode after generation.

## Verification before "done"

A ruthless-mentor pass is "done" when:
- All user-stated decisions enumerated and color-coded
- Each non-gold attack has a 1-paragraph specific reasoning + bulletproof rebuild with cost
- Updated plan reflects the revisions in concrete form (table, ticket list, or diffed doc)
- Open questions are yes/no decisions, not open-ended explorations
- "What we should do RIGHT NOW" proposes the cheapest validating probe
- User can see exactly which items are settled (GOLD) so they don't relitigate
