# Sprint 4 — Turn 2 Fix Contract

**Context:** Sprint 4 PRs `recipes-api#2` and `recipes-skill#2` are open. Copilot reviewed and posted 12 inline comments. Independent sec audit found 1 HIGH + 5 MEDIUM + 3 LOW. **Both reviews converged on the migration gap** — that is the deploy blocker.

**Goal:** apply ALL fixes below with tests for each, keep tests green, push to existing branches. Do not open new PRs.

**Working dir:** `/home/adam/repos/recipes-api` on branch `agent/tori/recipes-api-sprint4-carousel-telemetry`. Activate venv: `source .venv/bin/activate`. Run tests with `WR_DATABASE_URL=sqlite:///:memory: DATABASE_URL=sqlite:///:memory: pytest -q --ignore=tests/test_sandbox.py`. Should be 228 passing on entry, target ≥240 passing on exit.

---

## HIGH — must fix (deploy blockers)

### F1. Migration missing slot/verdict/install_event_id columns
**File:** `alembic/versions/a7f7db696591_typed_telemetry_and_carousel.py`
**Problem:** Model declares slot/verdict on CarouselEntry and install_event_id on TelemetryEvent, but migration only adds role/score and skill_id/goal_class/duration_seconds/retry_count/user_intervention/agent_class_hash. Production deploy via `alembic upgrade head` will create columns mismatching the model — first cron run + first carousel/telemetry write crashes.
**Fix:**
- Add `op.add_column('carousel_entries', sa.Column('slot', sa.Integer(), nullable=True))`
- Add `op.add_column('carousel_entries', sa.Column('verdict', sa.String(32), nullable=True))`
- Add `op.add_column('telemetry_events', sa.Column('install_event_id', sa.String(36), nullable=True))` (string for sqlite compat — same as skill_id)
- Mirror in `downgrade()` with `op.drop_column` calls
- Add a regression test: import the migration, run it, assert all 4 columns exist via `inspect(engine).get_columns('carousel_entries')` and `.get_columns('telemetry_events')`. File: `tests/migrations/test_columns_match_model.py`

### F2. Skill model has duplicate column definitions
**File:** `app/models.py` around line 126 (and again ~213-216)
**Problem:** Skill class has `vertical`, `rating_avg`, `install_count`, `is_free` declared twice with potentially conflicting nullable/default settings. SQLAlchemy treats later assignments as overrides; one of the definitions wins silently.
**Fix:** Find the duplicates, keep one block per column with the right defaults from the migration (`vertical default 'horizontal'`, `is_free default False`, `install_count default 0`, `rating_avg nullable`). Remove the other.

### F3. Migration / model UUID-vs-String mismatch on skill_id
**Files:** `alembic/versions/a7f7db696591_typed_telemetry_and_carousel.py` (uses String(36)), `app/models.py` (TelemetryEvent.skill_id UUID type)
**Problem:** Migration column type doesn't match model. On Postgres, UUID column receives string → driver casts implicitly but FK constraints may reject. On sqlite, String works either way. Choose ONE: either keep String(36) (works everywhere, matches install_events.skill_id pattern) and update the model, OR keep UUID on model and change migration to `postgresql.UUID(as_uuid=True)`. **Recommended:** keep String(36) in migration AND model — that's what install_events does. Update model to `Column(String(36), ...)`.

---

## MEDIUM — fix all

### F4. /api/carousel/* blocked by APIKeyMiddleware (sec audit #2)
**Files:** `app/middleware.py`
**Problem:** Carousel is meant to be public; currently 401s anonymous traffic.
**Fix:** Add a `PUBLIC_PREFIXES` tuple to `APIKeyMiddleware` containing `/api/carousel/`. In `dispatch()`, if `request.url.path.startswith(PUBLIC_PREFIXES)`, skip api-key validation. Add test in `tests/test_carousel_endpoint.py` that asserts unauthenticated GET returns 200 (or 404 if no entries) — NOT 401.

### F5. Telemetry skill enumeration oracle (sec audit #3)
**File:** `app/routes.py` post_telemetry handler
**Problem:** Resolving skill_slug against ALL skills lets authenticated callers tell private/draft skills apart from non-existent (201 vs 404).
**Fix:** Filter the skill query: `Skill.is_public == True OR Skill.creator.user_id == api_key_user_id`. If is_public=False and caller isn't creator/admin, return 404 (NOT 403, that's also an oracle). Add 2 tests: (a) caller telemetry with another user's private skill → 404, (b) caller telemetry with their own private skill → 201.

### F6. Carousel cron race condition (sec audit #4)
**File:** `app/carousel/cron.py`
**Problem:** Check-then-act idempotency check is not atomic; two concurrent runs both pass the gate.
**Fix:** Either (preferred) add a unique index on `carousel_entries(featured_date, slot)` in the migration AND catch IntegrityError to no-op, OR wrap the function in a `SELECT ... FOR UPDATE` advisory lock. Take the unique-index path — simpler. Don't forget the migration column for `slot` from F1; the unique index references it. Add test that runs the cron twice in quick succession and asserts only 7 rows ever exist.

### F7. goal_class no max_length on schema (sec audit #6)
**File:** `app/schemas.py` TelemetryIn
**Problem:** Pydantic doesn't bound goal_class length; migration column is `VARCHAR(64)`. A 65-char value passes Pydantic validation, hits Postgres, gets a `DataError` → 500.
**Fix:** Add `Field(max_length=64)` to `goal_class`. Add test: 65-char goal_class → 422 (NOT 500).

### F8. TelemetryIn.skill_slug missing validation (Copilot #4 / #7)
**File:** `app/schemas.py`
**Problem:** Empty/whitespace strings bypass intended 404-on-unknown logic.
**Fix:** Add `Field(strip_whitespace=True, min_length=1)` or use Annotated[str, StringConstraints(...)]. Add test: empty string → 422.

### F9. Empty dict payload silently NULL'd (Copilot #7)
**File:** `app/routes.py`
**Problem:** `if body.payload:` treats `{}` as falsy → NULL stored. Should preserve empty dict semantics.
**Fix:** Change to `if body.payload is not None: payload=json.dumps(body.payload) else: payload=None`. Add test: payload={} → DB stores `"{}"` text (not NULL).

### F10. _has_same_category_older actually checks for any same-category (Copilot #11)
**File:** `app/carousel/selector.py`
**Problem:** Function returns True for ANY other public skill in the same category, not specifically older. Misnames intent and may misroute the role assignment.
**Fix:** Add `created_at < skill.created_at` to the query. Handle NULL (treat NULL as oldest). Update tests in test_carousel_scoring.py role-assignment tests if needed.

---

## LOW — fix all (small, no excuse)

### F11. db_session fixture commits leak across tests (Copilot #1, #8)
**File:** `tests/conftest.py`
**Problem:** Outer `connection.begin()` + tests calling `session.commit()` defeats rollback isolation. State leaks between tests.
**Fix:** Use the SAVEPOINT pattern — `connection.begin_nested()` plus event listener that restarts the SAVEPOINT after each commit. SQLAlchemy docs reference: "Joining a Session into an External Transaction". Then tighten the carousel "404 when empty" test (Copilot #8) to assert exactly 404, not `in (200,404)`.

### F12. Migration tests hardcode .venv/bin/alembic (Copilot #2, #3)
**Files:** `tests/migrations/test_upgrade.py`, `tests/migrations/test_baseline_idempotent.py`
**Problem:** Hardcoded path breaks CI and clean-machine.
**Fix:** Replace with `[sys.executable, '-m', 'alembic', ...]` form. Verify tests still pass.

### F13. 404 detail leaks date string (sec audit #8)
**File:** `app/carousel/routes.py`
**Problem:** Error response echoes user-supplied date back. Tiny risk but easy fix.
**Fix:** Generic message: `detail="No entries for that date"` without echoing the date.

### F14. carousel_verdict.py hardcoded DB URL (sec audit #9)
**File:** `app/crons/carousel_verdict.py` (if exists)
**Problem:** Hardcoded `postgresql://wisechef@127.0.0.1:6432/wiserecipes` fallback. Should use settings.
**Fix:** Replace with `os.environ.get('WR_DATABASE_URL', settings.DATABASE_URL)`.

---

## Out of scope (do not do)

- Adding new endpoints
- Production deploy (Tori does this in Turn 3)
- Rewriting tests not flagged
- Changing the v4 plan

---

## Done definition

- All 14 fixes applied with regression tests
- `pytest -q --ignore=tests/test_sandbox.py` shows ≥ 240 passing, 0 failing
- Each fix is a separate commit message: `fix(F1): <short>`, `fix(F2): <short>`, etc.
- Push to origin
- Write `SPRINT4_TURN2_FIXES.md` listing each fix + commit SHA + test that proves it
- DO NOT touch production. DO NOT merge.
