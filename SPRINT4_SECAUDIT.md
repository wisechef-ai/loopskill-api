# Sprint 4 Security Audit — `agent/tori/recipes-api-sprint4-carousel-telemetry`

**Auditor:** Security subagent (Code Critic mode)  
**Date:** 2026-04-28  
**Branch:** `agent/tori/recipes-api-sprint4-carousel-telemetry`  
**Scope:** Diff vs `main`. Files audited: `app/carousel/routes.py`, `app/carousel/selector.py`, `app/carousel/cron.py`, `app/routes.py`, `app/schemas.py`, `app/middleware.py`, `app/main.py`, `app/models.py`, `alembic/versions/4ba0bf05cd47_baseline.py`, `alembic/versions/a7f7db696591_typed_telemetry_and_carousel.py`, `app/crons/carousel_verdict.py`.

---

## Conclusion

**1 HIGH finding** (migration gap; will break production on first deploy), **5 MEDIUM findings**, **3 LOW findings**. No findings in path-traversal or SQL-injection classes — the date regex, ORM parameterisation, and Pydantic validators are correctly implemented.

---

## Findings

---

### FINDING 1

- **Severity:** HIGH
- **Class:** broken-migration / missing-column
- **Location:** `alembic/versions/a7f7db696591_typed_telemetry_and_carousel.py:91-100` vs `app/models.py:213-214` vs `app/carousel/cron.py:52` vs `app/carousel/routes.py:101`
- **Scenario:**  
  The `a7f7db696591` migration adds only `role` and `score` to `carousel_entries`. However, the SQLAlchemy model declares **four** new columns on that table: `slot`, `role`, `verdict`, `score` (see `app/models.py` lines 213-216). The daily cron (`daily_carousel_job`) writes to `slot` explicitly at `cron.py:52`:
  ```python
  slot=item["slot"],          # 1-indexed D1 column
  ```
  The carousel read path at `routes.py:101` reads `e.slot`:
  ```python
  slot=e.slot if e.slot is not None else e.position + 1,
  ```
  Tests pass because they use `Base.metadata.create_all()` (SQLite in-memory), which creates the full model schema. On **production PostgreSQL**, after `alembic stamp 4ba0bf05cd47 && alembic upgrade head`, neither `slot` nor `verdict` will exist. The first cron run will crash with `ProgrammingError: column "slot" of relation "carousel_entries" does not exist`. Every subsequent GET `/api/carousel/today` or `/api/carousel/{date}` will also fail with the same error because SQLAlchemy generates `SELECT ... slot ... FROM carousel_entries`. The carousel feature is completely dead on day one of the production deploy.
- **Recommended fix:**  
  Add the two missing columns to the `upgrade()` function in `a7f7db696591_typed_telemetry_and_carousel.py` before the `carousel_entries` section:
  ```python
  op.add_column(
      'carousel_entries',
      sa.Column('slot', sa.Integer(), nullable=True,
                comment='1-indexed slot in today carousel (1..7)')
  )
  op.add_column(
      'carousel_entries',
      sa.Column('verdict', sa.String(32), nullable=True,
                comment='promote | hold | archive — set by verdict cron')
  )
  ```
  And correspondingly in `downgrade()`:
  ```python
  batch_op.drop_column('verdict')
  batch_op.drop_column('slot')
  ```

---

### FINDING 2

- **Severity:** MEDIUM
- **Class:** auth-misconfiguration (broken access control — over-restriction)
- **Location:** `app/middleware.py:62-64` vs `app/main.py:33`
- **Scenario:**  
  The `APIKeyMiddleware.EXEMPT_PATHS` set contains only hardcoded management/doc paths:
  ```python
  EXEMPT_PATHS = {
      "/docs", "/openapi.json", "/redoc", "/healthz", "/", "/api/healthz",
  }
  ```
  The carousel router is mounted at `prefix='/api'` in `main.py:33`, placing endpoints at `/api/carousel/today` and `/api/carousel/{date}`. Neither path — nor the `/api/carousel/` prefix — appears in `EXEMPT_PATHS` or `JWT_AUTH_PREFIXES`.  
  **Effect:** Any unauthenticated client (Astro landing page, browser, third-party integration, public user) hitting `GET /api/carousel/today` receives HTTP 401. The Sprint 4 contract explicitly designates carousel as the **public** skill catalog (no API key required — the data is intentionally public). This means the marquee Sprint 4 feature is currently entirely inaccessible to anonymous users in production.  
  **Secondary risk:** If a downstream caller works around this by embedding an API key in client-side JavaScript, that key becomes publicly visible.
- **Recommended fix:**  
  Add `/api/carousel/` as an exempt prefix in `APIKeyMiddleware`:
  ```python
  # Public catalog endpoints — no API key required
  PUBLIC_PREFIXES = (
      "/api/carousel/",
  )
  
  async def dispatch(self, request: Request, call_next):
      path = request.url.path
      # ... existing checks ...
      if any(path.startswith(p) for p in self.PUBLIC_PREFIXES):
          return await call_next(request)
      # ... API key validation ...
  ```
  Also add `/api/carousel/` to `RateLimitMiddleware`'s exempt set if it should be rate-limited differently, or leave it to hit the standard rate-limit (acceptable for public reads).

---

### FINDING 3

- **Severity:** MEDIUM
- **Class:** information-disclosure (private resource enumeration)
- **Location:** `app/routes.py:442-448`
- **Scenario:**  
  The `post_telemetry` handler resolves `skill_slug` to `skill_id` without filtering on `is_public`:
  ```python
  skill = db.query(Skill).filter(Skill.slug == body.skill_slug).first()
  if not skill:
      raise HTTPException(status_code=404, detail="unknown skill_slug")
  ```
  This creates a side-channel: an authenticated caller (any valid `x-api-key`) can enumerate **private/unreleased skill slugs** by sending `POST /api/telemetry` with guessed slugs. A 201 response proves the slug exists (even if the skill is private); a 404 proves it does not. With a wordlist and automated requests, all private skill names are discoverable within the rate-limit window. Private skills may include pre-launch products, internal tools, or client-specific workflows.
- **Recommended fix:**  
  Filter to public skills only when resolving for telemetry, OR return a generic 422 (not 404) when the slug is absent/private so the oracle response is uniform:
  ```python
  # Option A: public-skills-only resolution
  skill = db.query(Skill).filter(
      Skill.slug == body.skill_slug, Skill.is_public == True
  ).first()
  if not skill:
      raise HTTPException(status_code=422, detail="invalid skill_slug")
  
  # Option B: universal 422 regardless of existence
  # Replace the 404 with 422 and same message for both not-found and private cases
  ```
  Option A is preferred; it also prevents anonymous telemetry from incrementing counters on private skills.

---

### FINDING 4

- **Severity:** MEDIUM
- **Class:** race-condition (duplicate write / idempotency failure)
- **Location:** `app/carousel/cron.py:32-59`
- **Scenario:**  
  `daily_carousel_job` uses a check-then-act pattern without a DB-level uniqueness guarantee:
  ```python
  existing_count = db.query(CarouselEntry).filter(...).count()
  if existing_count > 0:
      return 0  # idempotency guard
  # ... compute and insert 7 rows ...
  db.commit()
  ```
  Two concurrent cron executions (e.g., overlapping systemd timer, application restart during cron, or parallel worker processes) can both execute `.count()` and observe `0` before either commits. Both then insert 7 rows, yielding 14 `carousel_entries` rows for the same date. The read path queries by date range without any `DISTINCT` or `LIMIT 7`, so `GET /api/carousel/today` returns 14 results with duplicated slots (two slot=1, two slot=2, etc.), corrupting the UI carousel.  
  No test currently covers concurrent execution; the idempotency test only validates sequential calls.
- **Recommended fix:**  
  Add a `UNIQUE` constraint on `(skill_id, featured_date)` or `(slot, featured_date)` in the migration, and catch `IntegrityError` in the cron as the idempotency signal:
  ```python
  # In migration: add unique constraint
  op.create_unique_constraint(
      'uq_carousel_slot_date', 'carousel_entries', ['slot', 'featured_date']
  )
  ```
  ```python
  # In cron.py: catch IntegrityError as idempotency signal
  from sqlalchemy.exc import IntegrityError
  try:
      db.commit()
  except IntegrityError:
      db.rollback()
      return 0  # concurrent run already committed
  ```
  Alternatively, use a PostgreSQL advisory lock (`SELECT pg_try_advisory_lock(...)`) around the entire check-insert block.

---

### FINDING 5

- **Severity:** MEDIUM
- **Class:** dos-resource-exhaustion (unbounded memory + CPU)
- **Location:** `app/carousel/selector.py:131-141`
- **Scenario:**  
  `select_top_7` fetches **all eligible skills** into Python memory in a single query with no `LIMIT`:
  ```python
  candidates = (
      db.query(Skill)
      .filter(...)
      .all()   # ← no LIMIT
  )
  ```
  All `N` skills are then scored in Python (`O(N)` CPU), sorted (`O(N log N)`), and only then truncated to 7. With a large catalog (10,000+ public skills), each cron execution loads the entire skills table into memory, JSON-deserialises every row, computes scoring math for each, and sorts all of them. This block also runs synchronously in the request cycle if anyone calls the cron endpoint directly.  
  Additionally, `_has_same_category_older` is called for each of the top 7 selected skills (`selector.py:84-97`), executing up to 7 extra DB queries per cron run. These are not N+1 at scale but are unindexed on `(category, is_public)`.
- **Recommended fix:**  
  Score in the database using a SQL expression to pre-filter candidates, or add a `LIMIT` at the DB layer before Python scoring:
  ```python
  # Short-term: cap candidates loaded into memory
  candidates = (
      db.query(Skill)
      .filter(...)
      .order_by(Skill.install_count.desc(), Skill.created_at.desc())
      .limit(500)   # heuristic pre-filter; tune based on scoring variance
      .all()
  )
  ```
  Long-term: push the full scoring formula into a SQL computed expression or materialised view. Also add a composite index `CREATE INDEX ix_skills_public_category ON skills (is_public, category)` to speed up `_has_same_category_older`.

---

### FINDING 6

- **Severity:** MEDIUM
- **Class:** input-validation / unhandled-db-error (schema mismatch → HTTP 500)
- **Location:** `app/schemas.py:96` vs `alembic/versions/a7f7db696591_typed_telemetry_and_carousel.py:62-63` vs `app/models.py:187`
- **Scenario:**  
  Three sources disagree on `goal_class` column width:
  
  | Layer | Width |
  |-------|-------|
  | Pydantic schema (`TelemetryIn`) | **unlimited** — no `max_length` |
  | ORM model (`TelemetryEvent.goal_class`) | `String(128)` |
  | Alembic migration | `VARCHAR(64)` |
  
  A caller that sends `goal_class` with 65–128 characters passes Pydantic validation and the ORM assignment, then hits PostgreSQL's column constraint. SQLAlchemy raises `sqlalchemy.exc.DataError: value too long for type character varying(64)`. This exception is not caught by `post_telemetry`, resulting in an unhandled HTTP 500 response with a full stack trace rather than a clean 422.  
  Note: `retry_count` has a similar partial gap — no upper bound in schema; PostgreSQL `INTEGER` max is 2,147,483,647. Values above that also produce an unhandled `DataError`.
- **Recommended fix:**  
  Add `max_length=64` to both `goal_class` and align all three layers:
  ```python
  # app/schemas.py
  goal_class: str | None = Field(None, max_length=64)
  retry_count: int | None = Field(None, ge=0, le=10000)
  ```
  Also reconcile the ORM model to match the migration: `goal_class = Column(String(64), ...)`.

---

## LOW Findings

---

### FINDING 7 (LOW)

- **Severity:** LOW
- **Class:** data-integrity / model-correctness
- **Location:** `app/models.py:113-116` and `app/models.py:122-125`
- **Scenario:**  
  `Skill` has four columns defined **twice** in the same class body (`vertical`, `rating_avg`, `install_count`, `is_free`). Python class bodies process statements top-to-bottom; SQLAlchemy processes `Column` assignments on the metaclass. The second definition silently overwrites the first. The two copies have differing `nullable` and `server_default` values for `install_count` (line 115: `nullable=True, default=0` vs line 124: `nullable=False, server_default="0"`). SQLAlchemy uses the last definition; the first set is dead code. This is a latent correctness risk if someone edits one copy but not the other. It also causes the ORM to potentially generate incorrect DDL via `create_all` in testing.
- **Recommended fix:**  
  Remove the duplicate block (lines 113-116) — keep only the authoritative Sprint 4 definitions at lines 122-125. Use a comment to document origin.

---

### FINDING 8 (LOW)

- **Severity:** LOW
- **Class:** information-disclosure (404 detail leaks validated date)
- **Location:** `app/carousel/routes.py:147-149`
- **Scenario:**  
  The `get_carousel_by_date` handler echoes the validated client-supplied date back in the 404 detail:
  ```python
  detail=f"No carousel entries for {date_str}",
  ```
  While `date_str` is already constrained to `YYYY-MM-DD` by the regex and `date.fromisoformat()`, echoing client input (even sanitised) in error messages is a low-grade information disclosure: it inadvertently confirms which dates have been probed. More practically, it is inconsistent with the `GET /carousel/today` 404 which does not echo. Severity is LOW because the format constraint is strict and no secrets are exposed.
- **Recommended fix:**  
  Use a static message:
  ```python
  detail="No carousel entries for the requested date",
  ```

---

### FINDING 9 (LOW)

- **Severity:** LOW
- **Class:** hardcoded-credential / config-hygiene
- **Location:** `app/crons/carousel_verdict.py:17`
- **Scenario:**  
  The verdict cron uses a hardcoded fallback database URL:
  ```python
  DATABASE_URL = os.environ.get("WR_DATABASE_URL", "postgresql://wisechef@127.0.0.1:6432/wiserecipes")
  ```
  If `WR_DATABASE_URL` is unset in any deployment environment (CI, staging, Docker compose with wrong env), the cron silently connects to the hardcoded host/port. This could cause a mis-targeting of the production DB if `127.0.0.1:6432` is reachable unexpectedly, or produce confusing silent failures if not reachable.
- **Recommended fix:**  
  Remove the fallback — fail loudly if the env var is absent:
  ```python
  DATABASE_URL = os.environ["WR_DATABASE_URL"]  # KeyError if unset = loud, correct
  ```
  Or raise with a descriptive message:
  ```python
  DATABASE_URL = os.environ.get("WR_DATABASE_URL")
  if not DATABASE_URL:
      raise RuntimeError("WR_DATABASE_URL must be set")
  ```

---

## Summary Table

| # | Severity | Class | Location | Short Description |
|---|----------|-------|----------|-------------------|
| 1 | **HIGH** | broken-migration | `a7f7db696591` migration:91 | `slot`+`verdict` missing from migration → production crash on carousel INSERT/SELECT |
| 2 | MEDIUM | auth-misconfiguration | `middleware.py:62` | Carousel public endpoints gated behind API key — anonymous access 401 |
| 3 | MEDIUM | information-disclosure | `routes.py:442` | `post_telemetry` enumerates private skill slugs via 201/404 oracle |
| 4 | MEDIUM | race-condition | `cron.py:32` | Non-atomic idempotency check allows duplicate carousel entry sets |
| 5 | MEDIUM | dos-resource-exhaustion | `selector.py:131` | Unbounded full-table skills scan in select_top_7 |
| 6 | MEDIUM | input-validation | `schemas.py:96` | `goal_class` no max_length; migration/model/schema mismatch → unhandled 500 |
| 7 | LOW | data-integrity | `models.py:113-125` | Duplicate column definitions in Skill model |
| 8 | LOW | information-disclosure | `carousel/routes.py:148` | 404 detail echoes client-supplied date string |
| 9 | LOW | config-hygiene | `crons/carousel_verdict.py:17` | Hardcoded fallback DB URL |

**No SSRF or open-redirect vectors found.** Path traversal on date param is correctly blocked (regex + `date.fromisoformat()`). All SQL queries use ORM parameterisation; no raw string interpolation in queries. Mass assignment is not a risk: `TelemetryIn` does not expose `id` or server-set fields.
