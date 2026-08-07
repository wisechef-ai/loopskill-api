# The defect loop: install → defect → patch → version → redeploy

Most registries publish artifacts. Most observability tools watch your own
processes. The thing neither does is close the circle across a **publisher/client
boundary**: a skill you published, running inside someone else's governed agent
fleet, reporting a real defect back to you privately, and then converging onto
your fix.

This document describes that loop, and — more importantly — states exactly how
much of it has actually been run.

---

## Status, stated up front

| Half | Claim | Status |
|---|---|---|
| install → defect → private sink | **private defect routing proven** | Verified at GitHub: a client agent's report reached a private repo, sink confirmed private by an anonymous `404`. |
| patch → version → redeploy → converge | mechanism shipped, **loop not yet run end to end** | The API surface and its terminal state exist and are covered by tests. The full five-step proof has not been executed against production. |

**Do not describe the first row as "the loop is proven end to end."** It is
private defect *routing*, which is the first half. The honest phrase is
"private defect routing proven". This table is the source of truth for that
wording; update it when — and only when — `scripts/moat_loop_proof.py` exits `0`
against production.

---

## The five steps

### 1. Install carries routable provenance

`GET /api/skills/install?slug=…&bundle_id=…`

The optional `bundle_id` attributes the install to the bundle it came from and
stamps `install_events.bundle_id`. That column is the *only* thing the feedback
rail routes on (`app/services/provenance.py::route_targets_for_provenance` →
`_curator_target`), so an install without it is structurally unroutable — the
report falls back to the platform's public default repo.

The parameter is validated: the bundle must exist and must actually **contain**
the skill, so an install cannot be misattributed to an unrelated bundle (which
would misroute somebody else's defect reports).

> **Testing note.** FastAPI ignores unknown query params silently, so calling
> this with the wrong parameter name returns `200` with the feature *not
> applied* — indistinguishable from working. Assert the
> `install_events.bundle_id` row, never the status code.

### 2. The defect routes to the curator's private sink

The client agent calls the `loopskill_feedback` MCP tool with the
`provenance_id` the install returned. The server resolves that token
**server-side** (it is a random opaque token carrying no metadata) to the exact
bundle the install came from, and dispatches a GitHub issue to that bundle
curator's configured repo.

> **Testing note.** `loopskill_feedback` returns `{"ok": true, "issue_url": ""}`
> when nothing was dispatched. Verify at the destination — the GitHub API — not
> at the tool's own success field. And confirm the sink is private with an
> *anonymous* probe (expect `404`), never by reading the repo's own settings.

### 3. The patch is published as a new version

`POST /api/skills/_publish` — an ordinary signed version publish. Nothing
loop-specific; the point is that the fix becomes an addressable version.

### 4. The member's bundle resolves to the new version

`POST /api/bundle-apply/{slug}/start`

The agent asks what its bundle should currently be running. The server resolves
every deployment to a concrete semver — the deployment's `version_pin` when set
(so a frozen bundle never silently drifts), else the skill's newest published
version — and opens a **persisted apply job** pinned to that resolution.

### 5. The member converges, terminally

`POST /api/bundle-apply/jobs/{job_id}/report`

The agent applies the bundle on its own host and reports the outcome per skill.
That report is the only thing that can move the job:

```
applying ──(any item reports 'failed')──────────────────> failed     [terminal]
         └─(every item reports 'success' AT THE EXPECTED
            semver)──────────────────────────────────────> converged [terminal]
```

Two properties make the green side falsifiable:

- **Convergence is version-equality, not assent.** An item counts only when the
  reported semver equals the expected one. An agent still running the defective
  version can report `success` all day and the job stays `applying`.
- **No vacuous convergence.** `all([])` is `True`, so an itemless job would flip
  straight to `converged` and prove nothing. Opening one is refused (`409`) with
  the unresolvable slugs named.

The bundle curator watches from the control plane via
`GET /api/bundle-deploy/{bundle_id}/jobs/{job_id}`.

> **What this replaced.** Until mesh_0408 W5, `apply` synthesized a `uuid4()`
> job id, discarded it, and the status endpoint answered a hard-coded
> `{"status": "applying"}` for *any* id, forever. Nothing could go red, and an
> id that was never issued read the same as a real one. A status that cannot go
> red is decoration, not observability.

---

## Running the proof

`scripts/moat_loop_proof.py` executes all five steps and asserts each one at its
observable rather than at a success field.

```bash
python scripts/moat_loop_proof.py --selftest   # falsify the harness first
python scripts/moat_loop_proof.py              # live run
```

Exit codes: `0` the full loop is proven · `1` a step failed (the output names
which) · `2` **VOID** — a precondition or control could not be evaluated. A `2`
is *not* a pass; the harness could not discriminate, so the run carries no
information either way.

`--selftest` drives the proof logic with stub transports engineered to produce
every outcome shape and asserts exits `{0, 1, 2}` are all reachable. Run it
before trusting a result: a harness whose failure branches have never executed
is not evidence. It also checks that the feedback payload varies between runs —
feedback deduplicates on a signature derived from the message text, so a fixed
test message first submitted while the rail was broken stays pinned to that
failed row forever, reporting RED long after the fix works.
