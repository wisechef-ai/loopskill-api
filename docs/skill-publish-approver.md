# `skill-publish-approver` — Human Sign-off Runbook

> **TLDR for admins:** Add the `approved` label to a `skill:publish-request` issue. The workflow fires automatically and publishes the skill. Do NOT run `gh workflow run` — the only supported trigger is the label event.

---

## How it works

The workflow (`skill-publish-approver.yml`) is triggered by **any `issues: labeled` event** on this repo. Its first guard is:

```yaml
jobs:
  approve-publish:
    if: github.event.label.name == 'approved'
```

Every label-add on every issue fires the workflow, but the job is **skipped** unless the label that was just added is exactly `approved`. This is intentional — it is not a bug.

### Why "skipped" appears in the run list

When any other label (e.g. `agent-reported`, `cla:auto-mit`, `skill:publish-request`) is added to any issue, GitHub fires the `issues: labeled` event, the workflow is queued, the job evaluates the `if:` guard, and immediately exits with `conclusion=skipped`. Two such skipped runs are recorded on 2026-05-25 when issue #290 received labels — the guard correctly rejected them because neither label was `approved`.

Confirmed on 2026-05-26 audit (repohygiene_2605/H2):

```
conclusion=skipped  event=issues  displayTitle="Feedback: pro cookbook tokens..."  (×2)
```

These runs are for issue #290, not #289. Issue #289 (`gbrain-on-hermes`) has not had any workflow runs because no `approved` label has been added to it — which is the correct state while under review.

---

## Tier-gating policy

| Tier | Who may approve | Mechanism |
|------|-----------------|-----------|
| `free` / `cook` | Tori (autonomous) can approve after gate pass | `gh issue edit --add-label approved` in a sub-agent |
| `operator` | Tori can approve after gate pass + 24h hold | same |
| `pro` / `pro_plus` | **Adam (human) must approve** | Adam adds `approved` label in GitHub UI |

`tier=pro` and above require human sign-off because:
1. Pro skills are installed by paying subscribers; a bad skill has higher blast radius.
2. The quality gate runs server-side at submission time but does NOT gate the final publish — a human reviewer is the last line of defence.
3. Automated approval of pro-tier skills would let a compromised agent fleet auto-publish to the catalog.

> **This is a deliberate design choice, not a gap.** If you want Tori to auto-approve pro skills, file a separate design issue.

---

## Review checklist before adding `approved`

Before adding the label, verify all of the following:

- [ ] **Tarball integrity** — the tarball stored at `request_id` matches the content in the issue body. If the submitter posted a correction comment (as happened with #289), the 24h dedup window must clear and the submitter must re-submit with corrected content before you approve the original request.
- [ ] **Quality gate** — run `python scripts/skill_quality_gate.py <skill_dir> --publish` against the extracted tarball. Zero BLOCK findings required.
- [ ] **Pipe-to-shell** — if `install.sh` uses `curl | bash`, verify a `# Rationale: <reason>` comment is present and the target URL is a well-known project (e.g. official Bun installer at `bun.sh/install`).
- [ ] **Internal hostnames** — scan `references/` and `templates/` for internal machine names, internal IPs, or internal UUIDs.
- [ ] **Supply-chain pins** — any `bun install -g github:<user>/<repo>` or `pip install git+https://...` should reference a specific tag or commit SHA.
- [ ] **License** — `license` in frontmatter must match the upstream project. MIT + MIT upstream: ✅. MIT + GPL upstream: ❌.

---

## Approving (Adam's action)

1. Open the `skill:publish-request` issue on GitHub.
2. Read the quality gate output in the most recent reviewer comment.
3. If **APPROVE**: click **Labels → approved** in the GitHub sidebar. The workflow fires within ~30s.
4. If **REJECT**: add label `publish-rejected`, close the issue with a comment listing specific remediation items.

No CLI command is needed. The label is the trigger.

---

## If the workflow fails after approval

The workflow comments on the issue with the specific error (e.g. `_publish call failed: HTTP 422`) and adds `publish-rejected`. You can:

1. Read the error, fix the underlying issue (usually a bad tarball or missing secret).
2. Remove `publish-rejected`, remove `approved` (so the guard re-evaluates cleanly), re-add `approved`.

Or if the tarball itself is wrong: ask the submitter to re-submit via `recipes_publish_request`, then approve the new issue.

---

## Secrets required

| Secret | What it is |
|--------|-----------|
| `RECIPES_MASTER_KEY` | Platform master API key — set in repo secrets |
| `RECIPES_API_BASE` | API base URL (defaults to `https://recipes.wisechef.ai`) |

Both must be present on `wisechef-runner` (the self-hosted runner). If either is missing, the workflow comments the error and adds `publish-rejected`.

---

## Audit trail

Every publish creates a `skill_versions` row in the DB and a closing comment on the issue with the `skill_id`, `version`, `sha256`, and `tarball_path`. This is the canonical audit record.

*Last updated: 2026-05-26 — repohygiene_2605/H2 audit by Tori.*
