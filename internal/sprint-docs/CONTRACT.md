# CONTRACT.md — Recipes Feedback Loop wire contract

This is the BINDING wire contract for Streams 1, 2, 4 of the
`recipes-feedback-loop-and-super-memory` sprint
(plan: `~/obsidian-vault/projects/wisebrain/2026-05-08-recipes-feedback-loop-and-super-memory.md`).

Every subagent in those streams MUST treat the schemas, routes, and event
shapes here as immutable. Only the controller (Tori) may amend this file.

Repo: `wisechef-ai/recipes-api`. Working dir on `wisechef-hq`:
`/home/wisechef/recipes-api-worktrees/<stream>/`. Each worktree has a `.venv`
symlink pointing at the canonical `/home/wisechef/wiserecipes-api/.venv` —
use it directly (do NOT `python -m venv` again).

DB: PostgreSQL via `app.database.SessionLocal` / `get_db`. Migrations via
alembic; current head is `0d8c25489899`. Add NEW migrations with `down_revision`
pointing at the current head at the time you run, OR at the new RCP migration
that ships in Stream 1. Stream-1 migration's revision id is fixed below to
prevent drift.

---

## Stream 1 — Feedback MCP tools (branch `feat/skill-error-mcp`)

### 1.1 New REST endpoints

Both new endpoints live in a new file `app/feedback_v1_routes.py`,
mounted in `app/main.py` as:

    from app.feedback_v1_routes import router as feedback_v1_router
    app.include_router(feedback_v1_router)

Router prefix: `/api/v1`. Auth: required via existing `APIKeyMiddleware`
(both endpoints require `x-api-key`). Public, no auth bypass.

#### POST /api/v1/recipify-request

Pydantic in:
    class RecipifyRequestIn(BaseModel):
        target_name: str = Field(min_length=1, max_length=128)
        why_useful: str = Field(min_length=1, max_length=2048)
        suggested_sources: list[str] = Field(default_factory=list, max_length=10)
        agent_id: str | None = Field(default=None, max_length=128)

Pydantic out:
    class RecipifyRequestOut(BaseModel):
        ok: bool
        id: str          # uuid of created row
        issue_url: str   # GitHub URL (filled when dispatch confirms)
        deduped: bool = False
        retry_at: datetime | None = None

DB table: `recipify_requests`
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid()
    target_name  TEXT NOT NULL
    why_useful   TEXT NOT NULL
    suggested_sources JSONB NOT NULL DEFAULT '[]'::jsonb
    agent_id     TEXT
    api_key_id   UUID  NULL  (resolved from middleware)
    signature    TEXT NOT NULL  -- sha256(target_name|why_useful) hex
    issue_url    TEXT
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    INDEX (api_key_id, created_at DESC)
    INDEX (signature)

GitHub dispatch: `event_type=recipify-request`, payload:
    {"id": "<uuid>", "target_name": "...", "why_useful": "...",
     "suggested_sources": [...], "agent_id": "...", "signature": "..."}

#### POST /api/v1/feedback

Pydantic in:
    class FeedbackIn(BaseModel):
        category: Literal["ux","search","billing","docs","install","other"]
        message: str = Field(min_length=1, max_length=4096)
        context: dict[str, Any] = Field(default_factory=dict)
        agent_id: str | None = Field(default=None, max_length=128)
        force: bool = False
        confirmation: str | None = Field(default=None, max_length=128)

Pydantic out:
    class FeedbackOut(BaseModel):
        ok: bool
        id: str
        issue_url: str
        deduped: bool = False
        last_submissions: list[dict] = Field(default_factory=list)
        retry_at: datetime | None = None
        force_available: bool = False

DB table: `feedback_submissions`
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid()
    category     TEXT NOT NULL
    message      TEXT NOT NULL
    context      JSONB NOT NULL DEFAULT '{}'::jsonb
    agent_id     TEXT
    api_key_id   UUID  NULL
    signature    TEXT NOT NULL  -- sha256(category|message) hex
    issue_url    TEXT
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
    INDEX (api_key_id, created_at DESC)
    INDEX (signature)

GitHub dispatch: `event_type=feedback`, payload:
    {"id": "<uuid>", "category": "...", "message": "...",
     "context": {...}, "agent_id": "...", "signature": "..."}

### 1.2 Multi-window rate limit (server-side, in `app/feedback_v1_routes.py`)

ALL three of the following windows enforced (any one fail → block).
Identity = `api_key_id` from middleware (fallback: `agent_id`, then peer IP).

  * dedup: identical signature, 2 hits / 7d → soft-block, return cached
    `issue_url` of original (`deduped: true`)
  * per-tool window: 10 distinct submissions per tool / 24h → hard-block
    (return last 3 with timestamps in `last_submissions`, `force_available: true`)
  * cross-tool ceiling: 30 total / 24h across recipify-request + feedback +
    skill-error → hard-block (`force_available: false`)
  * loop detector: ≥3 submissions in 5 min from same identity → 15 min
    cooldown, require `force=true` AND non-empty `confirmation` to override

Implementation lives in `app/feedback_ratelimit.py` and is called from BOTH
new endpoints AND extends the existing `app/skill_error_routes.py` 20/hr
backstop (raise to 30/hr; 30 cross-tool ceiling shares the same identity bucket).

Signature for skill-error reuse: `sha256(skill_slug|error_signature)`.

### 1.3 GitHub dispatch helper

New module `app/github_dispatch.py` with:

    def dispatch_event(event_type: str, payload: dict[str, Any]) -> str | None:
        """POST repository_dispatch to wisechef-ai/recipes-api.

        Returns the predicted issue URL ("https://github.com/wisechef-ai/recipes-api/issues/<unknown>")
        or None on failure. Never raises — failure logs and returns None so
        the API write is durable even if GitHub is down.
        """

Reads PAT from `os.environ["GITHUB_DISPATCH_PAT"]` (added to `.env` by
operator before deploy; on adam-xps `gh auth status` already has it; on
wisechef-hq the env file gets the same `gho_…` value).

Uses HTTPS to:
    POST https://api.github.com/repos/wisechef-ai/recipes-api/dispatches
    Authorization: Bearer <PAT>
    Accept: application/vnd.github+json
    Body: {"event_type": "<event_type>", "client_payload": <payload>}

Timeout 10s, single attempt, no retry (the workflow side will pick up later
events; we never want a slow GitHub to wedge the API thread).

### 1.4 Existing `/api/v1/skill-error` extensions (Stream 1)

Add to `app/skill_error_routes.py`:

  * Compute `signature = sha256(skill_slug|error_signature)` at intake.
  * After persistence, call `github_dispatch.dispatch_event(
        "skill-error", {"id": str(report.id), "skill_slug": ..., "error_signature": ...,
                        "agent_fp_anon": ..., "signature": ...})`.
  * Add the cross-tool ceiling check (call into `feedback_ratelimit`).
  * Bump local 20/hr → 30/hr to match plan.

### 1.5 Alembic migration (Stream 1)

File: `alembic/versions/a1b2c3d4e5f6_feedback_v1_tables.py`
Revision id: `a1b2c3d4e5f6`. Down revision: `0d8c25489899`.

Creates `recipify_requests` and `feedback_submissions` tables exactly
as specified above. Idempotent: wrapped in `IF NOT EXISTS` where possible.
Uses `op.create_index` for the two listed indexes per table.

### 1.6 New MCP tools (Stream 1)

In `app/mcp/tools/`:

  * `feedback.py` exporting `recipes_feedback(db, *, category, message,
       context=None, agent_id=None, force=False, confirmation=None,
       api_key_id=None) -> dict`
  * `recipify_request.py` exporting `recipes_request_recipe(db, *,
       target_name, why_useful, suggested_sources=None, agent_id=None,
       api_key_id=None) -> dict`
  * `skill_error.py` exporting `recipes_report_skill_error(db, *, slug,
       signature, summary, details=None, agent_id=None, api_key_id=None)
       -> dict` (wraps the existing `/api/v1/skill-error` logic by calling
       the same helpers — does NOT shell out to HTTP)

Each function reuses the same signature/ratelimit/dispatch helpers used
by the REST handlers. Output shape MATCHES the REST `*Out` schemas above
(plus tool-specific keys), serialized as JSON for the MCP `TextContent`
return.

Updates to `app/mcp/tools/__init__.py` (export the three new names) and
`app/mcp/server.py` (add tool definitions + dispatch branches).

Tool descriptions (verbatim, to teach the agent):

  * recipes_feedback: "Send feedback about recipes.wisechef.ai. Use when the
    user says 'write feedback that...', 'give feedback...', 'report that...',
    or expresses frustration with the platform UX, search, billing, or docs.
    Auto-creates a labelled GitHub issue. Rate limited per 24h."
  * recipes_request_recipe: "Request a new recipe (skill). Use when the user
    says 'recipify X', 'please add X to recipes', 'we need a recipe for X'.
    Creates a GitHub wishlist issue."
  * recipes_report_skill_error: "Report that an installed recipe is broken,
    has wrong instructions, or fails on this host. Use when the user says
    'this skill is broken', 'report this skill', or when an install/run
    fails. Auto-creates a labelled GitHub issue with the failure signature."

### 1.7 Tests (Stream 1)

`tests/test_feedback_mcp.py` — at least 8 cases:
  1. recipes_feedback happy path → 201 + issue_url
  2. recipes_feedback dedup (same signature within 7d) → ok=true, deduped=true,
     same issue_url returned
  3. recipes_feedback per-tool window (11th call in 24h) → hard-block,
     last_submissions populated, force_available=true
  4. recipes_feedback force=true override → bypasses per-tool block, succeeds
  5. recipes_feedback cross-tool ceiling (31st total) → hard-block,
     force_available=false
  6. recipes_feedback loop detector (3 in 5 min) → cooldown
  7. recipes_request_recipe happy path
  8. recipes_report_skill_error happy path with `RECIPES_REPORT_ERRORS=true`
     env (skip otherwise)
  9. github_dispatch failure → endpoint still returns ok=true with issue_url=""
     (durable write)

Mock `github_dispatch.dispatch_event` to a FakeDispatcher that records calls
and returns a deterministic URL. Use `tests/conftest.py` patterns from
`test_skill_error.py` and `test_feedback_incident.py`.

---

## Stream 2 — GitHub repo wiring (branch `chore/github-issue-templates`)

### 2.1 Files in `wisechef-ai/recipes-api`

Operate via `gh api` and direct file commits in this worktree:
`/home/wisechef/recipes-api-worktrees/feedback-templates/`.

Create:

  * `.github/ISSUE_TEMPLATE/recipe-bug.yml`
  * `.github/ISSUE_TEMPLATE/recipe-request.yml`
  * `.github/ISSUE_TEMPLATE/feedback.yml`
  * `.github/ISSUE_TEMPLATE/config.yml` — directs primary path to MCP tools,
    GitHub UI fallback
  * `.github/workflows/feedback-dispatcher.yml` — listens on
    `repository_dispatch` events of type `feedback`, `recipify-request`,
    `skill-error`. Opens a labelled issue with rich body using
    `actions/github-script@v7`.

### 2.2 Custom labels (`gh label create`)

  * `feedback`           color `0E8A16` desc "User-submitted feedback via MCP"
  * `recipe:request`     color `D93F0B` desc "Recipe addition request"
  * `recipe:bug`         color `B60205` desc "Existing recipe is broken"
  * `quality:hardcoded-path`  color `FBCA04`
  * `quality:missing-deps`    color `FBCA04`
  * `quality:wrong-os`        color `FBCA04`
  * `agent-reported`     color `5319E7` desc "Submitted by an autonomous agent"
  * `dedup`              color `C5DEF5` desc "Duplicate of another report"

### 2.3 Workflow body shape (`feedback-dispatcher.yml`)

For each event type, open ONE issue with:

  * Title: `[<category-or-event>] <truncated-message-or-target_name>`
  * Body: client_payload formatted as a markdown table + `signature` line at end
  * Labels: per-type fixed set
      - `feedback` → labels: `feedback`, `agent-reported`, plus
        `feedback/<category>` (also create those category sublabels)
      - `recipify-request` → labels: `recipe:request`, `agent-reported`
      - `skill-error` → labels: `recipe:bug`, `agent-reported`
  * If `client_payload.signature` matches an existing OPEN issue's title or body,
    add label `dedup` and link the previous issue. (Use `gh search issues` via
    `actions/github-script` `octokit.search.issuesAndPullRequests`.)

### 2.4 README — "How to give feedback"

Add a section with the three MCP tool names, copy-paste examples,
and the GitHub UI fallback.

### 2.5 Tests (Stream 2)

  * Manual e2e (documented in PR body, NOT in CI): one
    `gh api repos/wisechef-ai/recipes-api/dispatches -F event_type=feedback ...`
    call should open an issue with `feedback` label within 60s.
  * Workflow file `act` smoke (optional, only if `act` is on the host).

---

## Stream 4 — Graphify integration (branch `feat/recipify-graph-link`)

### 4.0 Pre-verify version pinning

PROBE these endpoints/tool semantics on `wisechef-hq` BEFORE writing
related-skills logic:

  * `GET /api/recipes/<slug>?version=1.0.0` — does the API support version-pinned reads?
  * `recipes_install` MCP tool — does it accept `slug@version`?

Save findings in the PR body. If absent → STOP and ESCALATE to controller
(Tori) before writing more code; controller will add Stream 4.5.

### 4.1 super-memory frontmatter contribution

Stream 4 does NOT publish super-memory (Stream 3 does). It DOES, however,
ensure `app/edge_builder.py` honours a new YAML field on every skill:
`related_skills: [<slug>, ...]`. Currently uses tags + category + co-install.

Re-tune weights so total ≤ 1.0 and existing rails change ≤20%:
  * 0.4 × declared_relation
  * 0.4 × jaccard tags  (was 0.6)
  * 0.1 × category      (was 0.2)
  * 0.1 × co-install    (was 0.2)
  * threshold 0.15 (unchanged)

Procedure:
  1. Compute current edge counts via a `--dry-run` flag (add one).
  2. Apply the new weights via the dry-run.
  3. Compare the two edge sets. If existing edges (those NOT involving
     a `related_skills` declaration) change by >20%, ABORT, dump the diff,
     fall back to old weights and surface declared_relation as an additive
     boost only (not a redistribution).

### 4.2 `recipes_install` MCP — surface related skills

In `app/mcp/tools/install.py`, on success, attach to the response:
  `"related_skills": ["<slug>", ...]`
where the list is the result of `GET /api/graph/related?slug=<slug>` (call
the existing internal function directly — do not shell out to HTTP).
Informational only; does not auto-install.

### 4.3 Tests

  * `tests/test_edge_builder_weights.py` — fixture with 3 skills, declared
    relations, prove new edges appear above threshold.
  * `tests/test_edge_builder_dryrun.py` — prove `--dry-run` flag emits
    diff JSON without writing.

---

## Stream 0 — already done

Service supervised by systemd unit `wiserecipes-api.service` (verified
2026-05-08 15:40 UTC; restart succeeded; running on origin/main HEAD
`5382807`). RCP-13 install_count fix is live; backfill is a no-op (counter
already in sync with telemetry).

Remaining for Stream 0: transparency endpoint + drift probe cron.
Tori will land these in `ops/count-fix-and-transparency` worktree.

  * `GET /api/health/transparency` — returns
    `{install_count_drift, skill_error_rate_7d, feedback_volume_7d,
      median_issue_resolution_h, last_backfill_at}`. JSON, no auth.
    Implemented as a new public route in `app/heartbeat_routes.py` or a
    new `app/transparency_routes.py`. Computed from live SQL each call
    (cache 60s in-memory).
  * Hourly drift probe `scripts/install_count_drift_probe.py` —
    diffs Skill.install_count vs. recomputed union of telemetry+install_events;
    fires `POST /api/v1/skill-error` when `drift > 0`.
  * systemd timer or cron entry. Use cron on `wisechef-hq` (matches existing
    `crontab -l` patterns).

---

## Stream 3 — super-memory (separate dir, NOT recipes-api)

Stream 3's worktree: `~/.hermes/skills/business/super-memory/`.
It does NOT touch the recipes-api repo. It produces a recipe (skill bundle)
that gets recipified into Tori's cookbook AFTER Stream 1+2+4 land.

Its only contract with this file: super-memory's SKILL.md frontmatter MUST include:

    related_skills: [cognee-v1-api-migration, cognee-litellm-proxy-rotation,
                     cognee-api-watchdog, cognee-nightly-ingest-optimization,
                     cognee-retrieval-architecture, cognee-llm-provider-swap,
                     cognee-minor-version-upgrade, vault-context-loader,
                     memory-dreaming]

so Stream 4's edge_builder picks it up and ≥7 of 9 land in the related view.

---

## Mandatory subagent prompt extras

Every implementer subagent in this sprint MUST be told:

  1. cwd is the worktree path; `cd` there before any file touch.
  2. Run tests via `cd <worktree> && PYTHONPATH=. .venv/bin/pytest -x <files>`.
  3. Commit each logical chunk via `git add ... && git commit -m "..."` AS YOU GO.
  4. Final commit must include any uncommitted files before returning.
  5. Output `SPRINT_DOCS/SUBAGENT_<STREAM>_OUTPUT.md` summarizing what changed.
  6. Do NOT push (controller pushes after review).
  7. Do NOT touch CONTRACT.md (controller-only).
  8. `gh pr create` is forbidden inside subagents (controller-only, after review).

