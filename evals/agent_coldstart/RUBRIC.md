# LoopSkill Agent Cold-Start Benchmark — Rubric

Authored blind to `app/` source; see `tasks.yaml` header for the exact public
surfaces consulted. This file defines how to score a run, not what the tasks
are (that's `tasks.yaml`).

## What is being measured

For each task, a fresh cold agent (blank `$HOME`, no `LOOPSKILL_API_KEY`, no
MCP config for `loopskill`, network on) is given the `prompt` verbatim, in
plain user language. The harness measures, per task:

- **pass/fail** — via the task's `success_check` only. No manual grading, no
  LLM-judge fallback. If `success_check` cannot run (network down, malformed
  seed, harness bug), that is an **error**, distinct from **fail**, and must
  not count against the product.
- **tool_calls** — count of discrete tool invocations the agent made
  (browser actions, shell commands, MCP calls, file writes — whatever the
  harness's tool-call accounting unit is) from task start to either success
  or timeout.
- **wall_minutes** — elapsed wall-clock time, capped at the task's
  `max_minutes`. A task that exceeds `max_minutes` is scored as a **timeout**
  fail regardless of whether it would have eventually succeeded.
- **tokens** — total LLM tokens consumed (prompt + completion) across the
  whole task attempt, if the harness's model wrapper reports it. Optional
  metric, not a pass/fail gate.

## Scoring a full run (all 10 tasks)

- **Suite pass rate** = passed / 10. Report the raw fraction, not just an
  aggregate score — a fixer improving 9→10 vs 5→6 are very different signals
  and should not be flattened into one number without the breakdown.
- **Cost-to-value** = for tasks that passed, median and p90 of `tool_calls`
  and `wall_minutes`. This is the actual product metric this benchmark
  exists to produce: cold-start friction, not just "does it work at all".
  A task that passes at 40 tool calls and 12 minutes is a different result
  than one that passes at 4 tool calls and 90 seconds, even though both
  count as 1/10 in the pass rate.
- **Never average tool_calls/wall_minutes across FAILED tasks into the
  cost-to-value numbers.** A fail that burns 60 tool calls flailing is not
  "expensive success" — it's a fail. Report failed-task effort separately,
  labeled as such (e.g. "median tool_calls burned on failed attempts: N"),
  since a high number there is itself useful signal about where an agent
  gets stuck without succeeding.

## Distinguishing FAIL from ERROR from BLOCKED

- **FAIL**: `success_check` ran to completion and returned nonzero. The
  agent attempted the task and the outcome did not meet the bar.
- **ERROR**: `success_check` itself could not execute for a reason unrelated
  to the agent's behavior (e.g. the verification harness's own network call
  failed, a seed file failed to materialize, a shell syntax error in this
  file). Errors must be logged and re-run before being counted; a systemic
  ERROR across all runs of one task is a signal the task itself is broken,
  not that the product regressed.
- **BLOCKED**: the task's declared `preconditions` were not actually met at
  start (e.g. harness accidentally pre-installed an MCP config). Blocked
  runs are discarded, not scored as fail.

## Anti-gaming discipline for whoever operates this suite

1. **`tasks.yaml` is read-only to any fix/patch loop.** The whole point of
   authoring it blind is that a fixer cannot special-case exact prompt
   wording or a specific `run_id` pattern once they see the check. If a
   proposed fix diff touches `evals/agent_coldstart/tasks.yaml`, reject it
   and treat the fix as out of scope — that file is the immutable test
   fixture, not implementation surface.
2. **Prefer fixes that generalize.** If a fix makes exactly one task's
   `success_check` pass without visibly improving the underlying capability
   (e.g. hard-coding a response for one slug, one run-id prefix, or one
   literal query string), that is textbook overfitting to the benchmark and
   should be flagged for human review even if the suite goes green. Rerun
   with a DIFFERENT run_id / different named skill (e.g. swap `humanizer`
   for another real public skill) as a spot-check before trusting a "fixed"
   result — this benchmark's `tasks.yaml` names specific skills/queries, but
   a genuine fix should visibly work for close variants too.
3. **A suite that goes from failing to 100% passing in one commit is a
   yellow flag, not a green light.** Read the diff. Real capability
   generalizes gradually or ships in one clearly-scoped feature; a sudden
   full clear from a small diff often means several checks got gamed by the
   same shortcut (e.g. one new "helpful" endpoint that special-cases every
   `coldstart-bench-*` and `rec_agent_*` shaped string it sees).
4. **Treat the `gameability_notes` field in `tasks.yaml` as a live checklist
   during review**, not just documentation. Several tasks (`self-register-
   agent`, `report-skill-error`, `install-public-bundle`, `tailor-fork-
   skill`) have explicitly stated open gaps where the check cannot fully
   distinguish a real fix from a narrow one. When a fix touches one of those
   task's surface area, the reviewer should specifically try the cheap
   cheat described in that task's `gameability_notes` and confirm it does
   NOT also pass, before accepting the fix as genuine.
5. **`report-skill-error` is the weakest check in the suite** (no public
   read-back exists for feedback submissions from a blind vantage point).
   Do not let a suite-wide pass rate hide the fact that this one task is
   measuring "the agent believes it succeeded," not "the report was
   durably recorded." Track it separately in longitudinal reports.

## What "done" looks like for a single benchmark run

A complete run report should include, at minimum:
- per-task: pass/fail/error/blocked, tool_calls, wall_minutes, tokens (if
  available)
- suite pass rate (N/10)
- cost-to-value median/p90 for passed tasks only
- the deleted candidate task and why (see `tasks.yaml` footer) — carried
  forward in every report so readers don't mistake "9 candidate tasks
  covered" for "10 originally planned, one silently dropped"
- any ERROR or BLOCKED runs, with cause, excluded from the scored total
