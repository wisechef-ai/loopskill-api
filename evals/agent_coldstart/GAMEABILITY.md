# GAMEABILITY.md — self-review of `tasks.yaml`

Written by the same author as `tasks.yaml`, immediately after drafting it,
specifically to hunt for ways the nightly FIX cron (which cannot edit
`tasks.yaml` itself, per the task brief) could still make a check go green
without shipping the real capability. Ranked roughly by how cheap the cheat
is relative to how hard it would be to detect after the fact.

## Top risks, ranked

### 1. `report-skill-error` — self-attested success (HIGH risk, NOT closed)
There is no public, unauthenticated read-back for a submitted skill-error
report. The check can only inspect a file the AGENT wrote about its own
belief that the report succeeded. A fixer could make the MCP tool always
return a response containing an id-like token — or even just NOT change the
backend at all and instead make the *agent's* default behavior more
confident about writing a plausible confirmation file — and this task would
go green with zero real change to error-report handling. This is the
weakest check in the suite by a wide margin. Not closeable from a blind,
public-only vantage point; flagged in both `tasks.yaml` and `RUBRIC.md` as
requiring separate scrutiny (read the actual fix diff, don't trust the
green check alone).

### 2. `self-register-agent` — key validated by shape/entropy, not identity (MEDIUM-HIGH, partially closed)
The differential check (a random but correctly-shaped `rec_agent_*` key must
be REJECTED while the real one is accepted) closes the cheapest version of
this cheat — a validator that accepts anything matching the regex. But a
fixer could still pass an allow-list keyed on "any key returned by
`/api/agents/register` in roughly the last hour," which is not the same as
a durable, revocable, per-identity key store. We have no public
`/api/admin/agent-identities` read (correctly — it's master-key gated) to
tell the difference from outside. Partially closed; fully closing this
would require an authenticated audit surface, which breaks the "blind,
public-only" authoring constraint by definition.

### 3. `install-public-bundle` — count threshold gameable by manual fan-out (MEDIUM)
The `>=10 SKILL.md files` + "mentions loopskill-essentials somewhere"
check can be satisfied by manually installing 10 arbitrary unrelated
skills and dropping one throwaway file with the bundle's name in it,
entirely bypassing the actual well-known bundle-install code path. A
tighter check (e.g. requiring the SAME provenance token or install
timestamp across all installed files) was considered but rejected: from a
blind, public-surface vantage point there was no confirmed, stable,
publicly-documented per-install marker to assert on (and inventing one
risks asserting on an implementation detail that doesn't actually exist,
which is worse than a loose bound). Left as a documented loose bound.

### 4. `publish-throwaway-skill` — slug-pattern special-casing (MEDIUM)
Using `{{run_id}}` in the slug prevents pre-seeding one specific known
answer, but does not prevent a fixer from special-casing the *pattern*
`coldstart-bench-*` — e.g. auto-approving/fabricating a catalog row for
anything matching that prefix without running it through the real
publish/moderation pipeline other skills go through. Cross-checking both
the detail endpoint AND the full-text search index raises the cost of a
naive fake (both surfaces must agree) but a determined narrow special-case
could still satisfy both cheaply. If this benchmark is run repeatedly, the
task's slug prefix should occasionally be rotated (a harness-level
concern, not a `tasks.yaml` edit) to keep any accumulated special-casing
from calcifying.

### 5. `tailor-fork-skill` — proves "changed", not "changed via LoopSkill's fork feature" (MEDIUM)
The hash-mismatch check proves the saved file differs from the current
public original. It cannot distinguish "the agent used LoopSkill's actual
tailor/fork MCP tool" from "the agent just hand-edited a locally cached
copy of the skill and never touched the product's fork feature at all." A
task that passes this way still demonstrates SOME real value (the agent
got a working private customization), just not necessarily via the
mechanism the prompt implies exists. Left open because from public
surfaces alone we could not confirm a distinguishing server-side artifact
(e.g. a fork-lineage field) is even part of the public contract.

### 6. `install-named-skill` — single-slug special case (LOW-MEDIUM)
Comparing the locally installed file's `tags:` line against the LIVE
catalog value (not a value frozen in this spec) forces content parity for
this one skill at verification time, but a narrow fixer could still special
case exactly the slug `humanizer` and nothing else. This risk is inherent
to naming any specific skill in a prompt; mitigated at the suite level by
`search-catalog-top3` and `install-unnamed-need` exercising install/search
against skills NOT named in advance, so a suite-wide special-case-only
strategy would still fail elsewhere.

### 7. `read-llms-txt-qa` and `uninstall-rollback-skill` — lower risk
Keyword-based comprehension checks and whole-`$HOME` sweeps are the most
resistant checks in the suite to narrow gaming, because they either require
matching several independent real facts (`read-llms-txt-qa`: needs a
service-type word AND a skill word AND a protocol word AND one of four real
install-command patterns, all at once) or search the entire filesystem
rather than a single known path (`uninstall-rollback-skill`). Residual gaps
noted directly in their `gameability_notes` in `tasks.yaml` are narrow
(template answers stuffing the right buzzwords; renaming a file's `name:`
field without changing its behavior) and considered acceptable for a v1
suite.

### 8. `search-catalog-top3` — lowest risk in the suite
Because the ground truth is fetched from the LIVE search index at
verification time rather than pinned into this file, there is no static
answer a fixer could special-case for the specific query `"code review"`
that wouldn't also require the real search index to keep genuinely working.
The main remaining risk — a fixer degrading ranking QUALITY while keeping
membership correct — is out of scope for a pass/fail gate and would need a
graded (not binary) metric to catch, which this suite deliberately does not
attempt (see RUBRIC.md's "never average across failed tasks" note for why
binary checks were chosen over graded ones: graded LLM-judge scoring is
itself gameable by whoever controls the judge prompt).

## Overall assessment

Of the 10 tasks, 7 have checks that require the underlying capability to
actually work generally (not just for one hard-coded input) or that
cross-validate against a live, independently-fetched ground truth. 3 tasks
(`report-skill-error`, `self-register-agent`, `install-public-bundle`) have
a meaningfully open gap that a narrow, benchmark-aware fix could exploit;
of those, `report-skill-error` is the one this benchmark cannot close from
outside the product at all, and should be weighted accordingly by anyone
reading a "10/10 passed" headline number — a "9/10 excluding
report-skill-error, plus report-skill-error passed a self-attestation check
only" framing is closer to the truth.
