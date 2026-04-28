# Sprint 4 Contract — Carousel + Typed Telemetry Substrate

**Date:** 2026-04-29
**Sprint owner:** Tori (orchestrator)
**Repos:** `~/repos/recipes-api` (FastAPI) and `~/repos/recipes-skill` (CLI)
**Branches:** `agent/tori/recipes-api-sprint4-carousel-telemetry` and `agent/tori/recipes-cli-sprint4-telemetry` (already checked out)
**Production:** `wisechef-agents:8200` behind `recipes.wisechef.ai`, FastAPI v0.4.0 — DO NOT TOUCH PROD until Turn 2 deploy step.

---

## INVARIANTS (do not violate)

1. **Schema migrations are additive only.** ADD COLUMN with default. NEVER DROP, NEVER ALTER TYPE, NEVER RENAME. Backfill must be `WHERE col IS NULL` style, no destructive ops.
2. **Backward compat:** existing `payload` text column on `telemetry_events` stays. Existing `position` column on `carousel_entries` stays. New columns are additions.
3. **Tests are required for every public endpoint and every scoring branch.** No untested code merges. Pytest must pass with `-x` (stop on first fail) before claiming done.
4. **Commit as you go.** After writing each file, `git add <file> && git commit -m "wip: <what>"`. Don't accumulate uncommitted state — Sprint 1 lesson.
5. **Working dir:** always pass `workdir=` to terminal calls. Don't rely on cwd.
6. **No prod writes:** subagents do not SSH to wisechef-agents. Tori main does the prod deploy in Turn 2.
7. **Use the existing test conftest** in `tests/conftest.py` — it has `client` and `db_session` fixtures already. Don't reinvent.
8. **Env vars:** read from settings.py, don't hardcode. Use `WR_*` prefix per existing convention.

---

## WIRE FORMATS (canonical)

### POST /api/telemetry (extended, NOT replaced)

Request JSON (typed mode):
```json
{
  "event_type": "task_completed",
  "skill_slug": "agent-rescue",
  "goal_class": "client-reporting",
  "duration_seconds": 42,
  "retry_count": 0,
  "user_intervention": false,
  "agent_class_hash": "sha256_short_hash"
}
```

Request JSON (legacy mode — must still work):
```json
{
  "event_type": "task_completed",
  "skill_slug": "agent-rescue",
  "payload": "{\"freeform\": \"data\"}"
}
```

Response: `{"status": "recorded", "event_id": "<uuid>"}` HTTP 201.

Validation:
- `event_type` ∈ {`install`, `first_use`, `task_completed`, `task_failed`, `replaced`}
- `skill_slug` must exist in `skills` table — server resolves to `skill_id` and stores both
- `goal_class` ∈ {`client-reporting`, `social-posting`, `seo-audit`, `proposal`, `agent-rescue`, `other`} — open enum, store text
- `duration_seconds` 0..86400 (24h cap), reject > 86400
- `agent_class_hash` regex `^[a-f0-9]{8,64}$` (we hash agent identity client-side)

### GET /api/carousel/today

Response:
```json
{
  "date": "2026-04-29",
  "entries": [
    {
      "slot": 1,
      "skill": {"slug": "agent-rescue", "title": "...", "category": "devops", "tier": "operator", "is_free": false, "vertical": "horizontal"},
      "role": "new-capability",
      "tagline": "Self-heal a stalled VPS in 60 seconds",
      "score": 8.4
    },
    ...
  ]
}
```

### GET /api/carousel/{YYYY-MM-DD}

Same shape. Date param strict `^\d{4}-\d{2}-\d{2}$` regex — no path traversal.

### CLI: recipes telemetry emit

```
recipes telemetry emit \
  --skill agent-rescue \
  --event task_completed \
  --goal-class client-reporting \
  --duration 42 \
  --retries 0 \
  --no-intervention
```

Reads `RECIPES_API_KEY` from env, posts to `${RECIPES_API_BASE}/api/telemetry`. Exit 0 on 201, exit 1 with stderr error message otherwise.

---

## SCORING ALGORITHM (D2)

```
score(skill, today) = 
    0.4 * log10(skill.install_count + 1)           # popularity, log-damped
  + 0.3 * recency_decay(skill.created_at, today)   # exp(-days/30)
  + 0.2 * (skill.rating_avg or 3.0) / 5.0          # quality, 0..1, default 3.0
  + 0.1 * (1.0 if skill.vertical=='agency' else 0.5) # vertical_match
```

Selector:
1. Filter `is_public = true OR (is_public IS NULL AND is_free = true)` — covers existing rows where is_public hasn't been set
2. Compute scores
3. Sort descending, take top 7
4. Assign slots 1..7 in score order
5. Assign `role`:
   - slot 1: `new-capability` if created_at within 30d, else `replaces` if exists a same-category older skill in db, else `experimental`
   - slots 2-5: `replaces` for same-category overlap, `new-capability` otherwise
   - slots 6-7: `experimental`
6. Tagline: first 80 chars of skill.description

Tie breaking: by `created_at DESC` then `slug ASC`.

---

## ALEMBIC SETUP (D1)

Bootstrap with:
```
cd ~/repos/recipes-api
.venv/bin/alembic init alembic
```

Then edit `alembic.ini` to read `sqlalchemy.url = ${WR_DATABASE_URL}` from env, and edit `alembic/env.py` to import `Base` from `app.models` and set `target_metadata = Base.metadata`.

First revision: `alembic revision -m "baseline"` — body left empty (no-op upgrade), this is the stamp target.
Second revision: `alembic revision -m "typed_telemetry_and_carousel"` — adds the columns.

Production protocol (Turn 2, NOT subagent's job):
```
ssh wisechef-agents
sudo -u postgres pg_dump wiserecipes > /tmp/pre-sprint4.sql
cd /home/wisechef/recipes-api && git pull
sudo -u wisechef .venv/bin/alembic stamp <baseline_rev>
sudo -u wisechef .venv/bin/alembic upgrade head
```

---

## TESTS

D1 (alembic):
- `tests/migrations/test_upgrade.py` — fresh sqlite db, run `alembic upgrade head`, assert schema state
- `tests/migrations/test_baseline_idempotent.py` — applying baseline to existing schema is a no-op

D2 (carousel):
- `tests/test_carousel_scoring.py` — known fixtures produce known scores
- `tests/test_carousel_endpoint.py` — happy path, date param injection rejection, missing date 404
- `tests/test_carousel_cron.py` — running twice for same date is idempotent

D3 (telemetry):
- `tests/test_telemetry_typed.py` — typed payload lands in typed columns
- `tests/test_telemetry_legacy.py` — legacy payload still works
- `tests/test_telemetry_validation.py` — bad event_type, bad goal_class, oversize duration, missing skill_slug

D4 (CLI):
- `tests/test_telemetry_emit.py` — CLI builds correct request, handles 201, handles errors

---

## DELIVERY DOCS

Each subagent writes one of:
- `~/repos/recipes-api/SPRINT4_D1_REPORT.md` (D1 owner)
- `~/repos/recipes-api/SPRINT4_D2_REPORT.md` (D2 owner)
- `~/repos/recipes-api/SPRINT4_D3_REPORT.md` (D3 owner)
- `~/repos/recipes-skill/SPRINT4_D4_REPORT.md` (D4 owner)
- `~/repos/recipes-api/SPRINT4_SECAUDIT.md` (sec audit owner)

Each report has: deliverables checklist, files touched, tests added, test pass output, known issues / TODOs.

---

## OUT OF SCOPE

Do NOT do:
- Client Reporter skill scaffolding
- Stripe Connect work
- New external endpoints beyond what's listed
- Astro landing page changes
- Cognee integration
- Production deploys (Tori does this in Turn 2)

If you find an issue out of scope, write it in your report and continue.

---

## ESCALATION

If you cannot proceed, write progress to your report and stop. Do not invent solutions to scope outside this contract. Tori will pick up Turn 2.
