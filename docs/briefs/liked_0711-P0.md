# P0 BRIEF — Liked primitive + auto-provision (liked_0711)

You are the IMPLEMENTER for Phase P0 of the `liked_0711` sprint on `wisechef-ai/loopskill-api`.
Full plan: `~/obsidian-vault/projects/recipes/plans/2026-07-11-liked-0711-execution-plan.md`.
A non-Codex reviewer (Opus) reviews your PR before merge — maker≠checker. Build it right; do not self-merge.

## GROUND-TRUTH (verified 2026-07-12 — the plan's own file refs are STALE, use THESE)
- The primitive is `Bundle` (table `bundles`), NOT "Cookbook" and NOT "bundle_stream.py". `Cookbook`→`Bundle` was renamed in Phase 3+4; `cookbook_limit()` kept its old name as a compat alias.
- `app/models.py:827` class `Bundle`. Owner column = `bundle_owner` (UUID, nullable, indexed) at line 846. Singleton flag pattern to MIRROR = `is_base` at line 842.
- Join tables already exist (do NOT create new ones): `BundleSkill` (models.py:902), `BundlePersonality` (1868), `BundleCompositeLoop` (1841).
- The tier cap lives in `app/bundle_routes.py:683-686` via `app.tier_labels.cookbook_limit(ctx.tier)` reading `config/tiers.yaml`. (P0 does NOT touch the cap — that's P5. Named only so you don't hunt for a nonexistent `bundle_stream.py`.)
- MIGRATION TEMPLATE TO CLONE: `alembic/versions/e9b5c7a3f1d8_drop_legacy_cookbook_columns_and_seed_base.py` — it creates a Postgres partial-unique index `WHERE is_base = true` AND seeds a row. Your migration mirrors it for per-owner Liked. Table is now `bundles` (the template still says `cookbooks` because it predates the rename — use `bundles`).
- Signup provision points (TWO): `app/auth_routes.py:160 github_callback`, `app/auth_routes.py:243 google_callback`. New users are created in these two handlers.
- `get_or_create` reference pattern for idempotent provisioning: `app/subscription_service.py:149 get_or_create_customer`.

## SCOPE (exactly this, nothing more — one PR)
1. **`Bundle.is_liked`** Boolean column, `nullable=False, default=False, server_default="0"` — clone the `is_base` column definition style verbatim.
2. **Migration** (new alembic revision, head-chained off current head — run `alembic heads` to find it):
   - Add the `is_liked` column to `bundles`.
   - Postgres partial-unique index `uq_bundles_is_liked_per_owner ON bundles (bundle_owner) WHERE is_liked = true` — guarantees ≤1 Liked bundle per owner. Mirror the `_has_index` guard + `IF NOT EXISTS` idiom from the template. SQLite branch: skip the partial index (SQLite in tests won't enforce it — the app-layer catch below is the real guard there).
   - **Backfill**: for every distinct `bundle_owner` that is non-NULL and has NO `is_liked=true` bundle, INSERT one Liked bundle (name `"Liked"`, `is_liked=true`, `is_base=false`, `bundle_owner=<that owner>`, `visibility='private'`). Idempotent: guard with `WHERE NOT EXISTS`. Do NOT touch or delete any existing bundle (freeze-not-delete; grandfathered payers may exceed their tier — that is fine and out of scope).
   - Alembic-only Postgres SQL is acceptable per repo convention, but the column add + backfill must run on SQLite too (tests use SQLite) — so column/table ops via op.* , the partial index via a `dialect == "postgresql"` branch only.
3. **Auto-provision helper** `ensure_liked_bundle(db, owner_id) -> Bundle` (put it next to the Bundle model logic, e.g. a new `app/liked_service.py` or into an existing service module — your call, keep it thin):
   - Idempotent: return the existing Liked bundle if present, else create it.
   - Call it from BOTH signup handlers (github_callback, google_callback) after the user row is committed.
   - Call it lazily as a safety net (a single well-placed call) so existing users who predate the migration also get one on first relevant touch — but the migration backfill should already cover them; the lazy call is belt-and-suspenders.
4. **Undeletable guard**: at the bundle-delete route layer (find it — grep `bundle_routes.py` for the DELETE handler), reject deletion of a bundle where `is_liked=true` with HTTP 403 and a clear message, exactly as `is_base` bundles are protected. If `is_base` deletion protection already exists, add `is_liked` to the same guard.

## ACCEPTANCE GATE (all must hold)
- Every user (new via either OAuth path + every existing user via backfill) has exactly ONE undeletable Liked bundle.
- Migration is idempotent: running it twice is a no-op; downgrade cleanly drops the column + index (do NOT delete Liked bundles on downgrade — leave data, just drop the schema objects; note this in the migration docstring).
- ZERO data loss on existing bundles.
- **CONCURRENCY (load-bearing):** two concurrent first-touch provisions for the SAME new owner must yield exactly one Liked bundle and zero 500s. Implement: attempt insert, catch `IntegrityError` on the partial-unique index, rollback-to-savepoint, re-read, return the existing row. Write a test that simulates the race (two sessions, same owner_id) and asserts one bundle + no exception escapes. **RED-PROOF it**: after green, temporarily neutralize ONLY the IntegrityError catch, confirm the test goes RED, restore, confirm GREEN. State in the PR body that you did this.
- Full suite green: `pytest -q` (baseline is 3381 passing — do not regress). Coverage ≥85% on changed lines.
- `pre-commit run --all-files` clean (ruff, ruff-format, bandit, mypy on the strict-scope modules if you touch them — you likely won't).

## DISCIPLINE (AGENTS.md — non-negotiable)
- ONE PR for P0. Branch `feat/liked-primitive-p0`. Do NOT modify `.coveragerc` or the coverage CI step.
- Do NOT touch god nodes (validate_key, APIKeyMiddleware.dispatch, recipes_install, SandboxRunner.run, scan_tarball). P0 shouldn't need them; if you think you do, STOP and note it in the PR instead.
- Any `except Exception:` needs a `# Rationale:` line above it. The IntegrityError catch should be narrow (`except IntegrityError`), not blanket.
- Keep a deviation log at `docs/deviations/2026-07-12-liked_0711-p0.md` — every place reality forced a change from this brief, one line each.
- Commit messages: intent-first (the repo uses a Lore trailer convention — a clean intent line + body is enough).
- When done: push the branch, open a PR against `main` titled `feat(liked): P0 — Liked bundle primitive + per-user auto-provision`, PR body lists changed files + the RED-proof confirmation + deviation-log link. Do NOT merge. Print `P0_DONE <pr-url>` as your final line.
