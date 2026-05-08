# SUBAGENT_S2_OUTPUT.md — Stream 2 GitHub Repo Wiring

Sprint: recipes-feedback-loop-and-super-memory
Stream: 2 (GitHub Issue Templates + Dispatcher Workflow + Labels)
Branch: chore/github-issue-templates
Worktree: /home/wisechef/recipes-api-worktrees/feedback-templates/
Executed by: subagent (Claude Code), 2026-05-08
Commit: d2810fc

---

## What was done

### 2.1 Issue Templates (.github/ISSUE_TEMPLATE/)

All 4 files created and committed:

  .github/ISSUE_TEMPLATE/config.yml
    - blank_issues_enabled: false
    - contact_links pointing at MCP tools (primary) and GitHub UI (fallback)

  .github/ISSUE_TEMPLATE/recipe-bug.yml
    - Prefill title: "[recipe:bug] "
    - Labels: recipe:bug, agent-reported
    - Fields: skill_slug, error_signature, summary, details, agent_id, os

  .github/ISSUE_TEMPLATE/recipe-request.yml
    - Prefill title: "[recipe:request] "
    - Labels: recipe:request, agent-reported
    - Fields: target_name, why_useful, suggested_sources, agent_id

  .github/ISSUE_TEMPLATE/feedback.yml
    - Prefill title: "[feedback] "
    - Labels: feedback, agent-reported
    - Fields: category (dropdown: ux/search/billing/docs/install/other), message, context, agent_id

### 2.2 Dispatcher Workflow (.github/workflows/feedback-dispatcher.yml)

Listens on repository_dispatch events:
  - feedback
  - recipify-request
  - skill-error

For each event:
  - Builds a rich markdown issue body (field table + signature line)
  - Applies per-event labels (see CONTRACT.md §2.3)
  - Runs dedup search via octokit.rest.search.issuesAndPullRequests using the
    client_payload.signature field; if a matching open issue is found, adds
    label "dedup" and links it in the body
  - For feedback events, auto-creates feedback/<category> sub-label if missing
  - Opens the issue via octokit.rest.issues.create

Label mapping (as per CONTRACT.md §2.3):
  - feedback        -> feedback, agent-reported, feedback/<category>
  - recipify-request -> recipe:request, agent-reported
  - skill-error     -> recipe:bug, agent-reported
  + dedup (appended when signature matches an existing open issue)

### 2.3 Custom Labels (gh label create --force, idempotent)

All 8 labels created on wisechef-ai/recipes-api:

  feedback               #0E8A16  "User-submitted feedback via MCP"
  recipe:request         #D93F0B  "Recipe addition request"
  recipe:bug             #B60205  "Existing recipe is broken"
  quality:hardcoded-path #FBCA04  "Skill has hardcoded paths"
  quality:missing-deps   #FBCA04  "Skill has missing dependencies"
  quality:wrong-os       #FBCA04  "Skill targets wrong OS"
  agent-reported         #5319E7  "Submitted by an autonomous agent"
  dedup                  #C5DEF5  "Duplicate of another report"

Used --force flag for idempotency (creates or updates without error if label exists).

### 2.4 README — "How to give feedback" section

Appended to README.md:

  - Table of 3 MCP tools with usage description
  - Copy-paste examples for recipes_feedback, recipes_request_recipe,
    recipes_report_skill_error
  - Rate-limit summary (dedup 7d, 10/24h per tool, 30/24h cross-tool,
    loop detector 3-in-5min cooldown)
  - GitHub UI fallback links to templates

### 2.5 Commit

Commit hash: d2810fc
Commit message: RCP-FB2: add GitHub issue templates, feedback-dispatcher workflow,
  8 labels, README feedback section
Files changed: 6 (5 new, 1 modified)

---

## E2E Test Result

Command run:
  gh api repos/wisechef-ai/recipes-api/dispatches \
    -F event_type=feedback \
    -F 'client_payload[category]=ux' \
    -F 'client_payload[message]=manual sprint test'

Result: HTTP 204 (accepted by GitHub API). No workflow run was triggered.

Root cause: repository_dispatch workflows are only triggered when the
workflow file exists on the repository's DEFAULT BRANCH (main). The
feedback-dispatcher.yml exists only in the local worktree commit on
branch chore/github-issue-templates, which has NOT been pushed per
task instructions ("do not push, do not open PR").

gh issue list --label feedback: (empty — no issue opened)

gh run list --repo wisechef-ai/recipes-api --limit 5:
  All listed runs are from prior CI/deploy workflow runs on main;
  no Feedback Dispatcher run appears.

Post-push verification steps (for controller):
  1. Push chore/github-issue-templates to origin (or merge to main).
  2. Re-run the dispatch command above.
  3. Wait 30-60 seconds.
  4. gh issue list --label feedback --repo wisechef-ai/recipes-api
     should show 1 new issue titled "[ux] manual sprint test".
  5. The issue body should contain a markdown table with id/category/message fields.
  6. Label "feedback/ux" will be auto-created if not present.

---

## Files Created / Modified

New files:
  .github/ISSUE_TEMPLATE/config.yml
  .github/ISSUE_TEMPLATE/recipe-bug.yml
  .github/ISSUE_TEMPLATE/recipe-request.yml
  .github/ISSUE_TEMPLATE/feedback.yml
  .github/workflows/feedback-dispatcher.yml

Modified files:
  README.md (appended "How to give feedback" section)

Output doc:
  SPRINT_DOCS/SUBAGENT_S2_OUTPUT.md (this file)

---

## Issues / Notes

- The dispatcher workflow uses actions/github-script@v7 with octokit object
  available implicitly; secrets.GITHUB_TOKEN has issues:write permission
  granted in the workflow-level permissions block.
- Category sub-labels (feedback/ux, feedback/search, etc.) are created
  on-the-fly by the workflow with color #0075ca. They are NOT pre-created
  by the label bootstrap script (only the 8 CONTRACT-specified labels are).
- The "feedback" label does not have a "color" keyword issue -- gh label create
  with --force was used to avoid conflicts with any pre-existing labels.
- No PR was opened. No push was executed. Controller must push after review.
