# ARCHITECTURE.md — LoopSkill open-core (Phase 0 spec freeze)

> **Frozen 2026-06-22 (loopskill_0622).** Companion to TERMINOLOGY.md. Defines
> the open-core boundary, the four catalog artifact types, and the self-host
> contract that the rest of the build must honor.

## The bet

LoopSkill is the **open-core "Spotify for AI agents"** — a self-hostable registry
of runnable agent artifacts. The whole product is OSS and runs from a clone;
monetization is the **hosted cloud** (we-run-it, managed sync, private registry),
never an OSS feature lockout. Goal: a thing you `git clone` and get value from
ALONE. Stars come from runnable artifacts, not a browsable catalog.

## Open-core boundary

| Layer | OSS (self-host) | Hosted-only (paid) |
|---|---|---|
| Registry API (FastAPI) | ✅ full | — |
| Catalog: skills, bundles, loops, personalities | ✅ full | — |
| Install / search / publish / deploy / sync | ✅ full | — |
| Sandbox runner (bwrap/firejail) | ✅ full | — |
| `docker compose up` one-command stand-up | ✅ | — |
| Managed sync / private registry / SSO / SLA | — | ✅ Pro $9.95 + enterprise |
| We run + scale + back up your registry | — | ✅ |

**Rule:** if removing a feature from the OSS repo would cost a GitHub star, it
stays in OSS. The paywall sells *convenience*, not capability.

## The four catalog artifact types

All four are first-class: **browse / upload / pull / deploy / feedback.**

| Type | Runnable? | What it is | v1 |
|---|---|---|---|
| **skill** | config | a single capability (SKILL.md + scripts/refs) | ✅ exists |
| **bundle** | config | a curated set of skills, deploy+sync to a fleet | ✅ exists (was cookbook) |
| **personality** | RUNNABLE | a deployable persona/SOUL (system prompt + config) | ✅ **v1 (pulled in)** |
| **loop** | RUNNABLE | a shareable, safety-bounded autonomous agentic loop | ✅ **v1 (pulled in)** |

The runnable types (loop, personality) are the star engine. A **loop** packages
the autonomous Plan→Act→Observe cycle as a shareable artifact:
`success_condition + verification_script + stopping_criteria (success/failure/budget)
+ system_prompt + tool_allowlist + max_turns budget`. No vetted, safety-bounded
loop registry exists in the wild — that white space is the strongest single lever
on the star count, which is why loop + personality are pulled into v1.

## Self-host contract (the clone-to-wow, Phase 1)

A developer clones the repo and within 60 seconds:
1. runs ONE command (`docker compose up` or `curl -fsSL loopskill.io/install | sh`),
2. with ZERO signup / zero credential,
3. browses a seeded starter catalog,
4. installs + deploys a bundle (or runs a loop) against their OWN local agent.

No wisechef.ai hard-coding may be required to self-host. Secrets boundary: a clean
`.env.example`; the server boots and serves the catalog with no secret set
(paid/hosted features degrade gracefully, never crash).

## Refactor-in-place, NOT greenfield

The rename rides on a **verified 2781-test green baseline** (measured 2026-06-22,
not the stale 2245 in the plan). Battle-tested subsystems — Stripe, auth/authz,
sandbox, federation, signed-URLs — are kept. The maintainability win is the
Phase-5 refactor the rename forces (consolidate bundle service to one SSOT), not
a rewrite. Discarding the suite to start over throws away encoded behavior of the
hard subsystems.

## Rename execution model

- Schema (P3) → contracts (P4) → refactor (P5) is a **strictly sequential spine**
  (each lands on clean names from the layer below). High-risk, single-threaded.
- Self-host (P1), loop+personality registries (P8, pulled to v1), brand (P2),
  portal (P7) are the **independent limbs** — parallelizable across worker agents.
- One phase = one PR. `terminology-lint` + green tests = the merge bar
  (local-CI parity + admin-merge if org CI blocks). Worker + reviewer harness.

## Compat / no orphaned installs

The live agentskills.io bridge (`/api/cookbooks/public/<slug>/.well-known/skills/`,
shipped 2026-06-13) and `cbt_` tokens are external contracts. Every renamed
contract keeps a **301/deprecation alias** through the 2–4 month parallel run
before the old `recipes.wisechef.ai` host retires (Phase 10).
