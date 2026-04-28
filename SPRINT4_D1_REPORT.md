# Sprint 4 D1 — Alembic Migrations Delivery Report

**Date:** 2026-04-28
**Branch:** `agent/tori/recipes-api-sprint4-carousel-telemetry`
**Deliverable:** D1 — Alembic migrations substrate

---

## Deliverables Checklist

- [x] `alembic init alembic` — bootstrapped Alembic in repo
- [x] `alembic.ini` — `sqlalchemy.url` updated; URL overridden from `WR_DATABASE_URL` env var in `env.py`
- [x] `alembic/env.py` — imports `Base` from `app.models`, sets `target_metadata = Base.metadata`, reads `WR_DATABASE_URL`
- [x] Revision 1: `4ba0bf05cd47_baseline.py` — empty no-op upgrade/downgrade (stamp target)
- [x] Revision 2: `a7f7db696591_typed_telemetry_and_carousel.py` — additive columns per contract D1
- [x] `app/models.py` updated with new columns (matching migration)
- [x] `tests/migrations/test_upgrade.py` — 11 tests, all pass
- [x] `tests/migrations/test_baseline_idempotent.py` — 6 tests, all pass
- [x] All migrations are additive only — no DROP, no ALTER TYPE, no RENAME
- [x] Each file committed separately

---

## Files Touched

| File | Action | Description |
|------|--------|-------------|
| `alembic.ini` | Created | Alembic config; sqlalchemy.url fallback + `WR_DATABASE_URL` override |
| `alembic/env.py` | Created | Reads `WR_DATABASE_URL`; imports `Base.metadata` from `app.models` |
| `alembic/README` | Created | Alembic auto-generated |
| `alembic/script.py.mako` | Created | Alembic auto-generated template |
| `alembic/versions/4ba0bf05cd47_baseline.py` | Created | Baseline revision — no-op, stamp target |
| `alembic/versions/a7f7db696591_typed_telemetry_and_carousel.py` | Created | Additive columns |
| `app/models.py` | Modified | Added Sprint 4 typed columns to `TelemetryEvent`, `CarouselEntry`, `Skill` |
| `tests/migrations/__init__.py` | Created | Package marker |
| `tests/migrations/test_upgrade.py` | Created | 11 migration upgrade assertions |
| `tests/migrations/test_baseline_idempotent.py` | Created | 6 baseline stamp + upgrade assertions |

---

## Columns Added (revision `a7f7db696591`)

### `telemetry_events`
| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `skill_id` | VARCHAR(36) | Yes | NULL | FK to skills.id (UUID as string) |
| `goal_class` | VARCHAR(64) | Yes | NULL | client-reporting\|social-posting\|seo-audit\|proposal\|agent-rescue\|other |
| `duration_seconds` | INTEGER | Yes | NULL | 0..86400 |
| `retry_count` | INTEGER | Yes | 0 | Retry counter |
| `user_intervention` | BOOLEAN | Yes | False | Was human input required? |
| `agent_class_hash` | VARCHAR(64) | Yes | NULL | hex 8–64 chars |

### `carousel_entries`
| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `role` | VARCHAR(64) | Yes | NULL | new-capability\|replaces\|experimental |
| `score` | FLOAT | Yes | NULL | Scoring algo output 0..10 |

### `skills`
| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `vertical` | VARCHAR(64) | Yes | NULL | agency\|solo\|enterprise\|horizontal |
| `rating_avg` | FLOAT | Yes | NULL | 0..5; scoring defaults to 3.0 when NULL |
| `install_count` | INTEGER | No | 0 | Denormalised counter for scoring |
| `is_free` | BOOLEAN | Yes | NULL | Carousel public filter flag |

---

## Tests Added

### `tests/migrations/test_upgrade.py` (11 tests)
- `test_alembic_version_is_head` — alembic_version shows `a7f7db696591`
- `test_telemetry_events_legacy_columns_intact` — 6 production columns present
- `test_install_events_columns_intact` — 7 production columns present
- `test_skills_legacy_columns_intact` — 13 production columns present
- `test_carousel_entries_legacy_columns_intact` — 6 production columns present
- `test_telemetry_events_new_typed_columns` — all 6 typed columns present
- `test_carousel_entries_scoring_columns` — role + score columns present
- `test_skills_scoring_columns` — vertical, rating_avg, install_count, is_free present
- `test_new_rows_accept_typed_telemetry` — can INSERT with typed columns
- `test_legacy_payload_column_still_writable` — legacy payload still writable
- `test_skills_install_count_defaults_to_zero` — install_count default = 0

### `tests/migrations/test_baseline_idempotent.py` (6 tests)
- `test_baseline_columns_intact_before_stamp` — no Sprint4 columns before migration
- `test_stamp_baseline_is_noop` — stamp leaves schema unchanged
- `test_alembic_version_after_stamp` — alembic_version shows baseline rev
- `test_upgrade_head_from_baseline` — upgrade adds all Sprint4 columns
- `test_legacy_data_survives_upgrade` — pre-existing rows readable after migration
- `test_alembic_version_at_head_after_upgrade` — head rev recorded after upgrade

---

## Test Output

```
============================= test session starts ==============================
platform linux -- Python 3.11.14, pytest-9.0.3, pluggy-1.6.0
plugins: anyio-4.13.0

tests/migrations/test_baseline_idempotent.py::TestBaselineIdempotent::test_baseline_columns_intact_before_stamp PASSED
tests/migrations/test_baseline_idempotent.py::TestBaselineIdempotent::test_stamp_baseline_is_noop PASSED
tests/migrations/test_baseline_idempotent.py::TestBaselineIdempotent::test_alembic_version_after_stamp PASSED
tests/migrations/test_baseline_idempotent.py::TestBaselineIdempotent::test_upgrade_head_from_baseline PASSED
tests/migrations/test_baseline_idempotent.py::TestBaselineIdempotent::test_legacy_data_survives_upgrade PASSED
tests/migrations/test_baseline_idempotent.py::TestBaselineIdempotent::test_alembic_version_at_head_after_upgrade PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_alembic_version_is_head PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_telemetry_events_legacy_columns_intact PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_install_events_columns_intact PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_skills_legacy_columns_intact PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_carousel_entries_legacy_columns_intact PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_telemetry_events_new_typed_columns PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_carousel_entries_scoring_columns PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_skills_scoring_columns PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_new_rows_accept_typed_telemetry PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_legacy_payload_column_still_writable PASSED
tests/migrations/test_upgrade.py::TestFreshUpgrade::test_skills_install_count_defaults_to_zero PASSED

============================== 17 passed in 7.08s ==============================
```

---

## Production Deploy Protocol (Turn 2, Tori's job)

```bash
ssh wisechef-agents
sudo -u postgres pg_dump wiserecipes > /tmp/pre-sprint4.sql
cd /home/wisechef/recipes-api && git pull origin agent/tori/recipes-api-sprint4-carousel-telemetry
sudo -u wisechef .venv/bin/alembic stamp 4ba0bf05cd47
sudo -u wisechef .venv/bin/alembic upgrade head
```

---

## Known Issues / TODOs

1. **`pytest -x -k migrations`** hits collection errors from unrelated tests (`test_auth.py`,
   `test_publisher.py` etc.) that need `jwt`, `stripe`, and other packages not in `.venv`.
   Workaround: run as `pytest -x tests/migrations/` directly. This is pre-existing — D1 does not own those tests.

2. **PostgreSQL `comment=` parameter**: The `comment=` kwarg in `sa.Column()` is Postgres-only;
   on SQLite it is silently ignored. Migration is correct for both dialects.

3. **`install_count` NOT NULL**: The migration specifies `nullable=False, server_default='0'`.
   On PostgreSQL this requires the server_default to backfill existing rows, which it does atomically.
   Contract invariant satisfied.

4. **`skill_id` FK in `telemetry_events`**: Stored as `VARCHAR(36)` (UUID string) rather than a
   typed UUID FK to maintain SQLite test compatibility. PostgreSQL will store it as TEXT and it
   remains joinable on `skills.id::text`. D3 owner should add a proper PG FK constraint if desired.
