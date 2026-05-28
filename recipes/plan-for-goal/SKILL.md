---
name: plan-for-goal
description: >
  Author a goal-execution plan-doc that is ready to paste into your goal/issue
  tracker as N discrete, falsifiable goals. Use when the conversation has produced
  a punch-list of decisions + scope and the next step is "ship it as a tracked
  sprint with budget, phases, premortem, and acceptance gates." Different from a
  generic markdown plan: plan-for-goal specifically closes with pre-formatted goal
  seeds and a single-paste "go <codename>" trigger so the same plan-doc drives
  execution. Aliased as `golazo` (Polish/Spanish slang for spectacular goal — the
  plan IS the goal).
tier: pro
category: planning
license: Apache-2.0
aliases: [golazo]
tags: [planning, goals, execution, plan-doc, budget, premortem, cost-discipline]
related_skills: [premortem, ruthless-mentor, brainstorming]
---

# plan-for-goal — write a goal-execution plan that ships

## When to use

Triggers:
- Decisions are locked on a punch list and you need a complete plan-doc with context, budget, phases, premortem
- You want the plan stored in `<your-knowledge-base>/projects/` so a future session or cron can resume
- A platform audit produced a P0/P1 list and the next step is to commit phases to a tracker
- Your team uses a `/goal` or issue-board flow and wants the seeds pre-formatted

## When NOT to use

- Pure brainstorm with no locked decisions → use `brainstorming`
- Generic markdown plan with no goal tracking → use a plain plan template
- Single-session task with no phases → just do it, don't bureaucratize
- The user hasn't asked for goals to be seeded → ask first; don't impose tracking on them

## Core principle

**Every plan has two halves that must both be present:**

1. **Context (the WHY block)** — verified state, what's locked, what was decided. This half lets a future session (or another teammate) resume cold without re-asking.
2. **Execution (the WHAT block)** — N phases with token budget per phase, model per phase, acceptance gates per phase, premortem, decision-defaults block, ship order, and a single-paste "go <codename>" trigger.

A plan with only context is a wiki article. A plan with only execution is a TODO list. plan-for-goal demands both.

## The 10-section plan-doc shape

```markdown
---
tags: [project, <product>, plan, goal-execution]
type: goal-execution-plan
status: ready-to-paste
codename: <slug>_<DDMM>           # e.g. mvp_1505
created: YYYY-MM-DD
updated: YYYY-MM-DD
author: <agent-name>
model: <model-that-wrote-this>
parent: "[[../<project>/00-index]]"
supersedes: [<prior plan wikilinks if any>]
related: [<prior recon/audit doc wikilinks>]
budget_cap_usd: <integer>
hard_stop_usd: <integer>           # budget_cap × 1.3
sequencing: <how phases run together>
---

# <Product> — `<codename>` (<one-line summary>)

> **Codename:** ...
> **Trigger:** <what the user said that started this>
> **What this is:** <one paragraph product spec for the sprint>
> **Budget unit:** USD per cost-reporting discipline.
> **Run order:** <parallel/serial breakdown>

## 0. What's locked (no further debate)
| # | Decision | Status |
| ... — every locked decision verbatim with ✅ |

## 1. Why this beats <prior state>
- Before/after diff in a code block
- New hero copy if applicable

## 2. Verified state (recon evidence, <timestamp> UTC)
- Live probe outputs in code blocks
- Drift / broken descriptions documented
- Tier/vocab/count drift listed

## 3. Plan structure — Phases A through <N>
For each phase:
### Phase X — <name> (<model>, ~$X.XX)
1. ... numbered execution steps ...
**Acceptance gates:**
- [ ] verifiable check 1
- [ ] verifiable check 2
**Budget:** $X.XX <model> · projection $Y.YY · cushion Z%

## 4. Total budget reconciliation
| Phase | Model | Budget | Projection | Cushion |
| ... |
| **Total** | | **$X** | **$Y** | **Z%** |

## 5. Pre-formatted goal seeds
```
1. "[<slug>/A] <one-sentence outcome>. Evidence: <falsifiable check>."
2. "[<slug>/B] ..."
...
N. "[<slug>/OUTCOME] <the durability goal>. THIS IS THE DURABILITY GOAL."
```

## 6. Premortem (pre-execution failure modes)
| # | What could break | L | I | LI | Mitigation |
| 1-N rows, sort by LI desc, top risk called out |

## 7. Decisions still needed (lightweight — defaults given)
| # | Decision | Default if no answer |
| Q1-QN; the human can edit defaults inline |

## 8. Ship order + worktree layout
- git worktree paths
- timestamp-based sequence

## 9. How to start execution
> **"go <codename>"**

## 10. References + evidence
- Live audit transcript link
- Recon probe locations
- Prior plan wikilinks
```

## Mandatory contents (the contract)

A plan-doc earns the label only if it has all of these:

1. **Codename** — `<slug>_<DDMM>` shape so future sessions can grep
2. **Budget cap in USD** in frontmatter (NOT calendar time, NOT agent-hours)
3. **Hard stop ceiling** at budget_cap × 1.3
4. **Section 0** lists every locked decision with ✅ and a verbatim quote when relevant
5. **Section 2** has actual live probe output — never assume, always verify
6. **Section 3** phases each carry: model name, token budget, projection, cushion %, acceptance gates as checkbox list
7. **Section 5** has N+1 goal seeds, with the last one marked `THIS IS THE DURABILITY GOAL` so the system knows which one closes the loop
8. **Section 6** premortem uses L×I scoring (1-10), sorted by LI descending, with the top risk highlighted in prose
9. **Section 7** decisions block uses default-if-no-answer pattern so the human can pass silently OR edit defaults inline
10. **Section 9** ends with a single-paste trigger phrase: `"go <codename>"`

## Pre-flight checks (do BEFORE writing the plan)

```bash
# 1. Confirm the knowledge-base location
ls <your-knowledge-base>/projects/<product>/ | head -5

# 2. Confirm a prior plan exists (this almost always supersedes one)
ls <your-knowledge-base>/projects/<product>/ | grep -i "$(date +%Y-%m)"

# 3. Check remaining token/cost budget — refuse to write a plan whose cap exceeds 1.2x remaining

# 4. If the trigger was an audit, READ the audit's findings before writing —
#    the plan is a structured response to them
```

## Link-entity audit (HARD RULE when external sources appear in the brief)

**Trigger:** When the brief says "add this to the catalog / marketplace / index / library" and includes one or more URLs, OR says "evaluate this project / repo / tool / skill" with a link.

**Rule:** BEFORE writing the plan body, classify each URL by entity type. The framing in the message may be wrong, and the plan inherits the bug unless you audit.

**Entity types to distinguish (these all look like "a repo" at a glance):**

| URL pattern | Real entity | What it means for the plan |
|---|---|---|
| `github.com/<org>/<repo>` | Single source repo | Standard "evaluate + ship recipe" path |
| `github.com/<org>` (no repo) | Organization | N repos — pick the relevant one(s), do not add the org as a unit |
| Decentralized git profile (DID-keyed, `*.eth`, `did:key:*`) | **Agent/identity profile**, NOT a repo | Owner of multiple repos — drill in to repo list, evaluate each separately |
| `npmjs.com/package/*`, `pypi.org/project/*` | Published artifact | Source repo is the linked-from field; install path is the package manager, not git clone |
| `*.ai`, `*.com` (SaaS) | Hosted product | Open-source repo may or may not exist; check for it in the footer/docs |
| `huggingface.co/<org>/<model>` | Model weights, not code | "Adding to catalog" means a wrapper recipe pointing to the model card, not bundling weights |
| Marketplace listing | Distribution endpoint | Source is upstream; the listing is a destination, not a thing to copy |

**Mechanics:**

1. For each URL in the brief, run `curl -sL <url>` (or the relevant API for github.com / npm / pypi) and inspect the response's actual title/metadata/entity type. Do not infer from the URL shape alone.
2. If the entity is an org / profile / DID / marketplace listing, **enumerate its contents** before treating any single item as "the thing to add." A profile with 6 repos is 6 separate go/no-go decisions, not one.
3. If the entity is upstream-published (npm/pypi/HF), **find the source repo** and audit license + activity + maintainer there.
4. Write findings into §2 (Verified state). For each URL, document: what the user thought it was, what it actually is, and the implication for the plan.
5. If the audit changes the architecture (e.g. "this isn't one catalog entry, it's 6 — and 5 are off-ICP"), surface it as the **first** attack in any ruthless-mentor pass.

**Anti-pattern:** "The user said add it, so I'll add it." If the URL's actual entity doesn't fit the catalog's data model (a profile vs a repo, an org vs a package), pushing it in anyway corrupts the catalog. Surface the mismatch, propose a federation/discovery path as the alternative, let the user decide.

## Source-recon before authoring Phase A (HARD RULE for code-shipping plans)

**Trigger:** When the plan involves authoring/modifying code in a repo you have not read the source of THIS session — even if you "know" the repo from a prior session or from memory.

**Rule:** Index the repo with a code-intelligence tool (gitnexus, semgrep, ctags) and read the topology BEFORE writing Section 3 (Phases). Then write Section 2 with concrete findings: top clusters, god-nodes by degree, existing tools / route surfaces / model fields, prior-art that overlaps. Phase scopes shrink to the *delta* the recon identifies.

**Why this matters:** Plans authored from memory systematically overscope because they re-invent existing surfaces. A v1 plan can assume "build hub federation from scratch" when in reality a sibling MCP tool already probes the local skill dirs and the right move is "extend, not build." The cost of recon ($0.05–$0.30) is asymmetrically smaller than authoring around stale assumptions.

### Mechanics

1. **Index (or refresh) the repo** (any code-intel tool that produces a topology: gitnexus, semgrep, ctags, or `find` + grep for tiny repos).
2. **Read repo-agent guidance files** (AGENTS.md, CLAUDE.md, .cursorrules) for repo-specific tool conventions.
3. **Dump topology:**
   - Top clusters by symbolCount → which functional areas exist
   - Top-degree functions/classes → architectural load-bearers (god nodes)
   - Top processes/flows → existing flows you'd otherwise re-invent
   - Symbol search for names in your plan (`search`, `install`, `auth`, table names) → confirms what's already there
4. **Read source for the 3-5 god-nodes** that overlap with your plan's scope. Get actual signatures, storage model, auth surface. Quote them in §2 with line numbers.
5. **Map your plan's deltas:**
   - For each Phase: "is the surface already there?" If yes, the phase shrinks to "extend X" not "build X."
   - For each new model field / column / table: "does this duplicate an existing schema?" If yes, reuse.
   - For each new endpoint / tool: "is there a sibling already?" If yes, register alongside it using the same pattern.
6. **Section 2 gets the actual findings** (file paths and line numbers). Section 3 phase scopes get rewritten with the deltas. If recon shrinks the budget by ≥30%, surface that — the user should know.

### When to skip

- Repo you've actively been editing this session and the recon would just re-confirm what you've seen
- Pure-design / no-code plan
- Plan whose Phase A is itself "index and read source" — already covered
- Repo size <50 files and the read is faster than the index

## Live-verify before authoring Phase A (HARD RULE for infra/migration plans)

When the plan involves a host, service, version, or third-party system you have not personally inspected this session, **Phase A is a live audit, not a wishlist of installs.** Do the audit BEFORE writing the plan body, then write Section 2 from real probe output, and shrink Phase A to the install + cleanup *delta* the audit identified.

**Mechanics:**

1. Do not assume connectivity — verify it. If the host is reachable only over a tunnel or password-auth SSH, get a working connection first.
2. Run a structured probe (hardware, OS, installed packages, running services, ports, disk free, RAM, target binaries, existing configs). Use **absolute paths** to installed tools — non-interactive SSH does NOT load `~/.zshrc` or `~/.bashrc`, so `which brew`, `which ollama` etc. can fail even when they're installed.
3. Write the audit output verbatim into Section 2.
4. Section 3 Phase A then becomes "install + cleanup the delta," typically much shorter than the draft — budget can shrink 40-60%.
5. Section 6 premortem is rewritten with the audit findings: risks confirmed get higher L scores, risks ruled out get reduced or removed, NEW risks (like disk-free) get added.

If §2 is empty hand-waving like "assume X, will verify later," you skipped the audit. Go back.

## Low-risk-first cutover ordering (migration plans)

When the plan has N agents/services being cut over to a new substrate (database, host, API endpoint), order the cutover **lowest-stakes first**, never alphabetically and never by importance.

For each candidate, score:
1. Data at risk (none = best, irreplaceable corpus = worst)
2. Path complexity (localhost < same-network < cross-WAN)
3. Recoverability (rebuild from source = best, restore from migrated snapshot = worse)
4. Production blast radius (idle/internal = best, user-facing = worst)

Cut over in ascending total. The **highest-stakes agent is the last cutover**, and that's the one that should get an explicit rollback script gated by a dry-run.

## Rollback-armed-before-final-cutover (HARD RULE for irreversible plans)

When the final cutover step is hard to reverse by hand in <60 seconds, the plan-doc MUST include:

1. A **rollback script** committed BEFORE the final cutover step runs. Not after.
2. A **`--dry-run` mode** on the rollback script that exits 0 only when all preconditions are satisfied (config backup exists, container is stopped-not-removed, etc.). The plan gates the final cutover on the dry-run printing `ROLLBACK READY`.
3. A **live test** of the rollback during the cutover phase: execute the rollback, verify the system works in rollback state, then forward-restore via a companion script. Both directions exercised means rollback is real, not theoretical.
4. **Stop, don't delete.** Docker containers get `docker stop`, never `docker rm`. Old install dirs get `mv to .local-archive-DATE/`, never `rm -rf`. Daily-backup window minimum 30 days.

Anti-patterns this prevents:
- "Just point the config back" — config is often one of three things that changed; missing the others means the rollback doesn't actually work.
- "We'll figure it out if it breaks" — under stress, at 11pm, with the user waiting, you won't.
- "I tested the dry-run, that's enough" — dry-run proves preconditions exist; only a live forward+back exercise proves the script works end-to-end.

## Runtime-constraint audit (composing architectures)

**Trigger:** When the plan assumes two systems will coexist (two memory providers, two webhook handlers, two cron schedulers, two auth backends, two anything-with-a-singleton-pattern), OR when the plan ports an external integration (PR, plugin, library) into an existing harness.

**Rule:** Before authoring §3 phases, READ THE TARGET HARNESS SOURCE for hard constraints. Specifically:
1. Find the manager/orchestrator class that will own the new component (`MemoryManager`, `PluginRegistry`, `CronScheduler`, `WebhookRouter`)
2. Read its docstring and the first 30 lines of source
3. Grep for words like `only one`, `single`, `exclusive`, `conflict`, `reject`, `at a time`, `per session`, `singleton`
4. If a constraint is found, the plan CANNOT assume parallel composition — pick a different architecture (hook plugin, sidecar process, separate slot) BEFORE writing phases

**Anti-pattern:** "I'll just install both and see what happens." The harness will silently reject the second registration and the plan will discover this during cutover, when rollback is hardest.

## Pitfalls

1. **Don't invent goals.** Every goal in §5 must map to a phase in §3. If there's no phase, there's no goal.

2. **Write the plan in the knowledge base FIRST.** Save to `<your-knowledge-base>/projects/<product>/YYYY-MM-DD-<slug>-<theme>.md`, THEN summarize in chat with a link. The vault is the canonical artifact.

3. **Don't promise calendar time.** USD token spend is the preferred unit. "ETA 2 hours" gets re-read as "you promised 2 hours." Use "ETA Phase A merge 22:00 UTC" only when sequencing matters, never as a deliverable promise.

4. **Don't skip the premortem.** Even on a "small" plan. The L×I matrix surfaces F8-class risks (doc describes broken flow, irreversible migration without backup, etc.) that no other section catches.

5. **Write goal seeds as declarative falsifiable outcomes, not instructions to yourself.** `"[slug/A] X re-tiered and Y kills shipped. Evidence: <SQL or curl that returns 0/200>."` NOT `"[slug/A] Re-tier 14 skills and delete 7."` The reader asks: did this happen, yes/no. Make the yes/no machine-checkable.

6. **Don't write phases without acceptance gates.** A phase without checkboxes is a vibe. It closes when the checkboxes go green, not when you say "done."

7. **Don't write decisions the user already answered.** §7 is for OPEN questions only. Re-listing answered ones is friction.

8. **Don't forget the cushion target.** Total cushion ≤30% over projection. If a phase has 50% cushion, your projection is too low. If it has 10%, your budget is too tight — bump it.

9. **Don't pick the most expensive model by default.** A mid-tier model covers 80% of phase work. Reserve the top model for: positioning copy, contract diffs, security-sensitive specs, irreversible deletions. Justify the choice inline.

10. **Don't end the chat reply with "want me to start?".** End with the trigger phrase: `"go <codename>"` or `"run A+E tonight, hold the rest"` — the user wrote a one-line trigger so they can ship in one keystroke.

## Verification after writing

```bash
# Plan exists at expected path
ls -la <your-knowledge-base>/projects/<product>/$(date +%Y-%m-%d)-*-<slug>.md

# Plan opens with the frontmatter (type: goal-execution-plan)
head -15 <plan-path>

# Section 5 has N+1 goal seeds (N = phase count + 1 for durability)
grep -c '^\d\+\. "\[' <plan-path>

# Section 9 ends with the trigger phrase
grep -E '\*\*"go [a-z0-9_]+"\*\*' <plan-path>
```

## After the user says "go <codename>"

1. **Run the goal seeds first** — for each seed in §5, create the corresponding tracker entry
2. **Confirm** — `[STATUS] <codename> goals seeded (N), starting Phase A`
3. **Execute Phase A** — never skip ahead; A often has a migration that B-N depend on

## Related skills

- `premortem` — imagine plan failed, work backward. Run **before** plan-for-goal if both apply.
- `ruthless-mentor` — stress-test the plan's locked decisions after writing.
- `brainstorming` — open generation mode; plan-for-goal is the closing mode.
