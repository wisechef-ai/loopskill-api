# SUBAGENT_S1_OUTPUT.md — Stream 1 Implementation Summary

**Branch:** `feat/skill-error-mcp`
**Date:** 2026-05-08
**Implementer:** Subagent S1 (claude-sonnet-4-6)

---

## What was implemented

All items from CONTRACT.md §1.1–§1.7 are implemented and committed on
`feat/skill-error-mcp`.

---

## Commits (in order)

1. `RCP-FB1: add github_dispatch helper module`
   - `app/github_dispatch.py` — `dispatch_event(event_type, payload)` helper.
     Reads PAT from `GITHUB_DISPATCH_PAT` env var. Never raises; logs warning
     and returns None when PAT is missing or GitHub fails. Returns the
     placeholder issue URL on success.

2. `RCP-FB1: add multi-window rate limiter (dedup/per-tool/cross-tool/loop)`
   - `app/feedback_ratelimit.py` — all four windows:
     * dedup: signature seen ≥1 time in 7d → soft-block, returns cached issue_url
     * per-tool: 10/24h per (identity, tool) → hard-block, force_available=True
     * cross-tool: 30/24h across all tools → hard-block, force_available=False
     * loop detector: ≥3 in 5 min → 15-min cooldown, overrideable with force+confirmation
   - Exports: `check_and_record`, `update_dedup_url`, `check_skill_error_backstop`,
     `make_signature`, `reset_all`

3. `RCP-FB1: add RecipifyRequest and FeedbackSubmission ORM models`
   - `app/models.py` — two new SQLAlchemy models appended at bottom:
     * `RecipifyRequest` → table `recipify_requests`
     * `FeedbackSubmission` → table `feedback_submissions`
   - Both have `id`, `signature`, `api_key_id`, `agent_id`, `issue_url`,
     `created_at` + table-specific columns. Two indexes each.

4. `RCP-FB1: alembic migration a1b2c3d4e5f6 - recipify_requests + feedback_submissions tables`
   - `alembic/versions/a1b2c3d4e5f6_feedback_v1_tables.py`
   - Revision: `a1b2c3d4e5f6`, down_revision: `0d8c25489899`
   - Idempotent (`CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`)
   - Creates both tables with all columns and indexes as specified in CONTRACT.md

5. `RCP-FB1: add POST /api/v1/recipify-request and POST /api/v1/feedback REST endpoints`
   - `app/feedback_v1_routes.py` — new router, prefix `/api/v1`:
     * `POST /api/v1/recipify-request` → `RecipifyRequestIn` / `RecipifyRequestOut`
     * `POST /api/v1/feedback` → `FeedbackIn` / `FeedbackOut`
     * Both apply all four rate-limit windows, persist durably, fire dispatch
   - `app/main.py` — `include_router(feedback_v1_router)` added

6. `RCP-FB1: extend skill_error_routes - composite sig, github dispatch, cross-tool ceiling, 30/hr`
   - `app/skill_error_routes.py` changes:
     * Added `import os` and `import hashlib` (was missing `os`)
     * Added `from app import github_dispatch, feedback_ratelimit` imports
     * Bumped `_RATE_LIMIT_MAX` from 20 → 30 per hour
     * Computes `composite_sig = sha256(skill_slug|error_signature)` after persist
     * Calls `github_dispatch.dispatch_event("skill-error", {...})` after commit
     * Adds cross-tool ceiling check via `feedback_ratelimit.check_and_record`

7. `RCP-FB1: add MCP tools recipes_feedback, recipes_request_recipe, recipes_report_skill_error`
   - `app/mcp/tools/feedback.py` — `recipes_feedback(db, *, category, message, ...)`
   - `app/mcp/tools/recipify_request.py` — `recipes_request_recipe(db, *, target_name, ...)`
   - `app/mcp/tools/skill_error.py` — `recipes_report_skill_error(db, *, slug, signature, ...)`
   - `app/mcp/tools/__init__.py` — three new exports added
   - `app/mcp/server.py` — three new `types.Tool` definitions + dispatch branches

8. `RCP-FB1: add 9-case test suite tests/test_feedback_mcp.py`
   - 9 tests, all passing (`PYTHONPATH=. RECIPES_REPORT_ERRORS=true .venv/bin/pytest -x tests/test_feedback_mcp.py`)

---

## Test results

```
9 passed in 1.30s (with RECIPES_REPORT_ERRORS=true)
8 passed, 1 skipped (without RECIPES_REPORT_ERRORS=true)
```

Test 8 (`test_skill_error_happy_path_mcp`) is correctly skip-guarded by
`@pytest.mark.skipif(os.environ.get("RECIPES_REPORT_ERRORS", "").lower() != "true", ...)`.

---

## Files created / modified

| File | Status |
|------|--------|
| `app/github_dispatch.py` | NEW |
| `app/feedback_ratelimit.py` | NEW |
| `app/feedback_v1_routes.py` | NEW |
| `app/mcp/tools/feedback.py` | NEW |
| `app/mcp/tools/recipify_request.py` | NEW |
| `app/mcp/tools/skill_error.py` | NEW |
| `alembic/versions/a1b2c3d4e5f6_feedback_v1_tables.py` | NEW |
| `tests/test_feedback_mcp.py` | NEW |
| `app/models.py` | MODIFIED (appended 2 new model classes) |
| `app/main.py` | MODIFIED (added import + include_router) |
| `app/skill_error_routes.py` | MODIFIED (os import, dispatch, cross-tool check, 30/hr) |
| `app/mcp/tools/__init__.py` | MODIFIED (3 new exports) |
| `app/mcp/server.py` | MODIFIED (3 tool defs + 3 dispatch branches) |
| `SPRINT_DOCS/SUBAGENT_S1_OUTPUT.md` | NEW (this file) |

---

## Notes for controller

- `GITHUB_DISPATCH_PAT` must be added to `.env` on `wisechef-hq` before deploying.
  When absent, the API write is still durable; dispatch just returns `issue_url=""`.
- The in-process rate-limit state (`app/feedback_ratelimit.py`) uses `threading.Lock`
  backed dicts. For multi-worker production use, swap to a Redis backend.
- The alembic migration uses raw `op.execute()` with `IF NOT EXISTS` for full
  idempotency. This works on PostgreSQL. SQLite tests use `Base.metadata.create_all`
  directly (the migration is not run in tests — this matches the existing pattern).
- Do NOT push or open a PR — controller handles that.
