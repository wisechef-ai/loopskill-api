# Sprint 4 D2 Report — Carousel Scoring + Selector + Endpoints + Cron

**Date:** 2026-04-29  
**Branch:** `agent/tori/recipes-api-sprint4-carousel-telemetry`  
**Status:** ✅ COMPLETE

---

## Deliverables Checklist

| Item | Status |
|------|--------|
| `app/carousel/__init__.py` | ✅ |
| `app/carousel/selector.py` — `score(skill, today)` + `select_top_7(db, today)` | ✅ |
| `app/carousel/cron.py` — `daily_carousel_job(db, today)` idempotent | ✅ |
| `app/carousel/routes.py` — GET /carousel/today + /carousel/{date} | ✅ |
| Router wired in `app/main.py` with prefix `/api` | ✅ |
| `tests/conftest.py` — `client` + `db_session` + `make_skill` fixtures | ✅ |
| `tests/test_carousel_scoring.py` ≥4 tests | ✅ (16 tests) |
| `tests/test_carousel_endpoint.py` ≥4 tests | ✅ (11 tests) |
| `tests/test_carousel_cron.py` ≥4 tests | ✅ (8 tests) |
| `pytest -x -k carousel` passes | ✅ 37 passed |
| D1 model columns added to `app/models.py` | ✅ |
| Commit-as-you-go | ✅ |

---

## Files Touched

### Created
- `app/carousel/__init__.py`
- `app/carousel/selector.py`
- `app/carousel/cron.py`
- `app/carousel/routes.py`
- `tests/conftest.py`
- `tests/test_carousel_scoring.py`
- `tests/test_carousel_endpoint.py`
- `tests/test_carousel_cron.py`
- `SPRINT4_D2_REPORT.md` (this file)

### Modified
- `app/models.py` — added D1 columns to `Skill` (`vertical`, `is_free`, `install_count`, `rating_avg`) and `CarouselEntry` (`slot`, `verdict`); these were needed so `Base.metadata.create_all` in tests creates them before the D1 migration file lands.
- `app/main.py` — `include_router(carousel_router, prefix="/api")`

---

## Scoring Algorithm

Implemented verbatim from contract §SCORING ALGORITHM:

```
score(skill, today) =
    0.4 * log10(install_count + 1)         # popularity, log-damped
  + 0.3 * exp(-days_since_created / 30)    # recency_decay
  + 0.2 * (rating_avg or 3.0) / 5.0       # quality, default 3.0
  + 0.1 * (1.0 if vertical=='agency' else 0.5)  # vertical_match
```

D1 fields (`vertical`, `is_free`, `install_count`, `rating_avg`) handled as optional (`None` → safe defaults) per contract instructions.

---

## Tests Added (37 total via `-k carousel`)

### `tests/test_carousel_scoring.py` — 16 tests
- `TestRecencyDecay` (4): today=1.0, 30d ago=e⁻¹, None=1.0, old→~0
- `TestScoreFormula` (8): zero installs baseline, 100 installs+agency, null install=0, horizontal=0.5, agency=1.0, rating default, monotonicity (installs, recency)
- `TestSelectTop7` (4): ≤7 returned, 1-indexed slots, descending scores, private excluded

### `tests/test_carousel_endpoint.py` — 11 tests
- `TestCarouselToday` (4): happy path, wire format shape, skill brief fields, 404 guard
- `TestCarouselByDate` (7): happy path, 404, invalid format → 422, path traversal rejected, malformed date → 422, slot ordering, date echo

### `tests/test_carousel_cron.py` — 8 tests
- Insert up to 7, fewer when fewer skills, idempotent second run → 0, multiple runs stable, entries have slot+role, entries have score≥0, no skills → 0, private skills excluded

### `tests/migrations/test_upgrade.py` — 2 (pre-existing, from D1 subagent, also selected by `-k carousel`)

---

## Test Pass Output

```
====================== 37 passed, 194 deselected in 4.34s ======================
```

All 37 tests pass with `pytest -x -k carousel`.

---

## Date Validation

`GET /api/carousel/{date_str}` uses a compiled regex `^\\d{4}-\\d{2}-\\d{2}$` (no path traversal possible). Invalid format raises HTTP 422. Then `date.fromisoformat()` validates it is a real calendar date (e.g. `2026-13-99` is rejected).

---

## Known Issues / TODOs

1. **D1 migration file** (`alembic/versions/a7f7db696591_typed_telemetry_and_carousel.py`) should add `slot`, `verdict`, `vertical`, `is_free`, `install_count`, `rating_avg` columns — the model already declares them; D1 subagent's migration will cover this.
2. **`app/crons/carousel_selector.py`** (legacy standalone script) remains unchanged — not in D2 scope. The new `app/carousel/` package supersedes it for the Sprint 4 API surface.
3. The existing `GET /api/carousel/today` in `app/routes.py` returns the legacy `CarouselEntryOut` schema (position-based). The new `app/carousel/routes.py` returns the Sprint 4 wire format with `slot`/`role`/`score`. Both are mounted — the new router's `/api/carousel/today` takes precedence because it is included second (FastAPI first-match wins — existing route in `router` at `/api/carousel/today` is mounted first but using the same path prefix; in practice both respond to the same path so Tori should decide which to keep in prod cleanup).
4. **`conftest.py`**: also added `make_skill()` helper function (importable from `tests.conftest`) needed by the D3 telemetry tests that were already written by the D3 subagent.
