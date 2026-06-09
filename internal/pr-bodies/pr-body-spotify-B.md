# feat(spotify_0608-B): public cookbook discovery + install-count integrity

**Plan:** `projects/recipes/2026-06-08-spotify_0608-execution-plan.md` · Phase B

Public discovery surface for cookbooks, built on the Phase A foundation.

> **Scope note (deviation from plan §2):** `slug`+`visibility` columns already
> shipped in Phase A (the Bucket→Cookbook data migration required them first), so
> B does **not** re-migrate them. B adds only its own net-new `is_test` integrity
> column. Flagged in plan §2.

## What changed

### §4.2 install-count integrity (the honest-counts moat)
- **New `api_keys.is_test` column** (migration `spotify_0608_b_apikey_is_test`,
  NOT NULL default false). Marks synthetic (test/CI/internal harness) keys.
- **`_install_counts_for`** now `OUTER JOIN api_keys` + filters
  `coalesce(APIKey.is_test, false) = false` — anonymous installs (null key) stay
  organic, only explicitly-test-keyed installs are excluded.
- **`_record_install_event`** skips the denormalized `Skill.install_count` bump
  for test-keyed installs (keeps the carousel popularity term honest at the
  source) — the `InstallEvent` audit row is **always** written regardless.
- This de-pollutes all three downstream surfaces that rank on install count:
  discovery ranking, future leaderboards (Ph G), and the GTM kill/scale signal.

### Public discovery endpoints (unauthenticated)
- **`GET /api/cookbooks/discover`** — ranked feed of `visibility='public'`
  cookbooks. `sort=installs` (default, by real 7d→total installs, test-excluded)
  or `sort=newest`. Paginated (`limit`≤100, `offset`).
- **`GET /api/cookbooks/public/{slug}`** — public cookbook page. 404 unless
  `visibility='public'`. Returns card + ordered skill list + a one-copy-paste
  **`clone_line`** (`recipes_cookbook_install from "cookbook://<slug>?ref=<creator>"`)
  so an agent can compose it straight off the public page (GTM gate, rendered in Ph F).
- **`?ref=<creator>` attribution** threaded onto every public card + clone line
  so install attribution is visible from week 1 (GTM build-plan mod #2).
- Both allowlisted in `middleware/api_key.py` as **specific sub-paths** — the bare
  `/api/cookbooks` CRUD stays auth-gated. Routes registered **before** the
  `/{cookbook_id}` catch-all (verified by test) so `discover`/`public` aren't
  captured as a cookbook id.

## Verification
- ✅ 8 new tests (`test_spotify_0608_b_discovery.py`): is_test exclusion in both
  count paths, audit-row-always-written, discover public-only + rank-by-real-installs
  + pagination, public page ?ref/clone_line + 404-on-private + 404-on-unknown.
- ✅ Migration `spotify_0608_b_apikey_is_test` verified **upgrade + downgrade on real
  Postgres 17** (column added NOT NULL default false, existing rows backfilled, drop clean).
- ✅ Route-order guard (discover/public before `/{cookbook_id}`).
- ✅ `pyfile-size-check` green — `api_key.py` kept ≤600 (condensed a pre-existing
  comment block to absorb the 4 new allowlist lines; net 598).
- ✅ Full suite green; ruff/ruff-format/bandit/mypy pass.

## Acceptance gate (plan §2 Phase B)
- [x] Anon user can browse public cookbooks + reach a public cookbook page
- [x] SEO-indexable public page renders (JSON surface; Ph F adds the static HTML)
- [x] Public cookbook URLs carry `?ref=<creator>` attribution
- [x] Discovery ranking + public count surface EXCLUDE test/CI installs
- [ ] Live-verify on prod (post-merge)
