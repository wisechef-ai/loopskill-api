# Phase A "Verifier rename" blast-radius interrogation

Review scope:
- Backend: `/home/adam/repos/loopskill-api`
- Portal: `/home/adam/repos/loopskill-portal`
- Mode: review only; no implementation.

## Executive verdict

**GO-WITH-CONDITIONS for a rename+alias PR only. NO-GO for doing the same-name handover to the new composite `Loop` in the same PR.**

Phase A should be split:

1. **Deploy 1: rename shipped `Loop` semantics to `Verifier` in code/docs/UI where safe, while keeping storage and old public/API/MCP aliases intact.** This deploy must not repurpose `/api/loops`, `/loops`, `loopskill_search_loops`, or `loopskill_get_loop` to a different object.
2. **Deploy 2 or later: introduce the new composite `Loop` on new explicit surfaces, then migrate public naming intentionally.** Only after old verifier aliases are proven live should `/loops` or `loop_*` names be claimed by composite loops.

The reason is concrete: the existing shipped `Loop` is not just an internal model. It is a public portal surface, a public no-auth API prefix, a runner, ratings, seed data, MCP discovery/dispatch, docs, and migrations with child tables.

## 1. Full blast radius of shipped `Loop`

### Backend model and tables

- `app/models.py:1276` defines `class Loop(Base)`.
- `app/models.py:1288` maps it to `__tablename__ = "loops"`.
- `app/models.py:1291-1318` defines the public contract columns: `slug`, `title`, `success_condition`, `verification_script`, `max_turns`, `budget_usd`, `stopping_criteria`, `tool_allowlist`, `system_prompt`.
- `app/models.py:1320-1328` stores counters and status: `install_count`, `run_count`, `rating_avg`, `rating_count`, `is_archived`.
- `app/models.py:1335-1345` defines relationships to `LoopVersion` and `LoopRating`.
- `app/models.py:1348` defines `class LoopRating(Base)`.
- `app/models.py:1357` maps ratings to `loop_ratings`.
- `app/models.py:1360-1362` has `loop_ratings.loop_id -> loops.id ON DELETE CASCADE`.
- `app/models.py:1371` defines `class LoopVersion(Base)`.
- `app/models.py:1372` maps versions to `loop_versions`.
- `app/models.py:1375` has `loop_versions.loop_id -> loops.id`.
- `app/models.py:1386` has `uq_loop_version`.

### Backend migrations

- `alembic/versions/loopskill_0622_p8_runnable_types.py:31-62` creates `loops` and its indexes.
- `alembic/versions/loopskill_0622_p8_runnable_types.py:63-77` creates `loop_versions` with FK to `loops.id`.
- `alembic/versions/loopskill_0622_p8_runnable_types.py:128-138` downgrades `loop_versions` then `loops`.
- `alembic/versions/lsk0627_loop_feedback.py:49-57` adds `run_count` and `rating_count` to `loops`.
- `alembic/versions/lsk0627_loop_feedback.py:60-89` creates `loop_ratings`.
- `alembic/versions/lsk0627_loop_feedback.py:94-99` creates Postgres partial unique index on `loop_ratings(loop_id, rater_user_id)`.
- `alembic/versions/lsk0627_loop_feedback.py:103-111` downgrades `loop_ratings` and loop counters.

Reconcile history is separate from loops:
- `app/models.py:748-774` defines `ReconcileEvent` on `reconcile_events`; it keys skill-version reconcile telemetry, not `Loop`.
- `alembic/versions/evergreen_e_reconcile_events.py:29-51` creates `reconcile_events`.
- `alembic/versions/f1b2c3d4e5a6_p34_rename_cookbook_to_bundle.py:57-62` renames `reconcile_events.cookbook_id -> bundle_id`. Any table-rename migration must not disturb this history or Alembic ancestry.

### Backend route surface

- `app/main.py:196-202` imports and includes `app.loop_routes.router` with tag `loops`.
- `app/loop_routes.py:1-10` documents `/api/loops` as the shipped loop registry.
- `app/loop_routes.py:39` creates `APIRouter(prefix="/api/loops", tags=["loops"])`.
- `app/loop_routes.py:42-66` serializes a `Loop` to `LoopOut`.
- `app/loop_routes.py:69-88` implements `GET /api/loops`.
- `app/loop_routes.py:91-122` implements `GET /api/loops/{slug}`.
- `app/loop_routes.py:125-183` implements `POST /api/loops` publish.
- `app/loop_routes.py:186-286` implements `POST /api/loops/{slug}/run`.
- `app/loop_routes.py:289-361` implements `POST /api/loops/{slug}/rate`.

### Public auth/middleware blast radius

- `app/middleware/_public_paths.py:21-27` puts `/api/loops` in method-agnostic `PUBLIC_PREFIXES`.
- `app/middleware/api_key.py:282-296` allows any `PUBLIC_PREFIXES` request through while stamping `auth_ctx` from key/JWT/anonymous.
- `tests/test_loopskill_portal_0627_public_loop_browse.py:35-41` pins `/api/loops` in the public allowlist.
- `tests/test_loopskill_portal_0627_public_loop_browse.py:44-51` pins prefix matching for `/api/loops` and `/api/loops/hello-world-loop`.
- `tests/test_loopskill_portal_0627_public_loop_browse.py:87-121` pins route-level auth on anonymous `run`, `rate`, and `publish`.

This is risky because a dual mount under `/api/verifiers` must duplicate the public-read behavior without accidentally opening writes.

### Backend schema/API response models

- `app/schemas.py:326` starts the loop/personality schema block.
- `app/schemas.py:329-354` defines `LoopOut`.
- `app/schemas.py:357-361` defines `LoopRateIn`.
- `app/schemas.py:364-370` defines `LoopRatingOut`.
- `app/schemas.py:373-380` defines `LoopDetailOut`.
- `app/schemas.py:383-399` defines `LoopPublishIn`.
- `app/schemas.py:402-423` defines `LoopRunIn`.
- `app/schemas.py:426-440` defines `LoopRunOut`.

### Backend validation and runner

- `app/loop_validation.py:1-18` documents the loop manifest as the safety-bounded contract gate.
- `app/loop_validation.py:31-35` defines `LoopValidationError` and `validate_loop_manifest`.
- `app/loop_validation.py:47-121` validates `success_condition`, `verification_script`, `system_prompt`, `max_turns`, `budget_usd`, `tool_allowlist`, and `stopping_criteria`.
- `app/loop_runner.py:1-20` documents current verify-mode and future LLM driver seam.
- `app/loop_runner.py:87-103` uses `WR_LOOP_RUN_WORKSPACE` and `WR_LOOP_RUN_REQUIRE_SANDBOX`.
- `app/loop_runner.py:107-140` defines `LoopRunResult`.
- `app/loop_runner.py:143-157` defines `LoopDriver`.
- `app/loop_runner.py:160-197` defines `LoopRunner`.
- `app/loop_runner.py:254-363` runs verification and returns bounded/sandboxed/refused results.
- `app/loop_runner.py:367-400` stages workspaces using `loop-run-...` temp dirs.

### Backend MCP tool definitions and dispatch

- `app/mcp/_registry_loopskill.py:12-43` advertises `loopskill_search_loops` and `loopskill_get_loop`.
- `app/mcp/tools/loopskill_catalog.py:15` imports `Loop`.
- `app/mcp/tools/loopskill_catalog.py:18-52` implements `loopskill_search_loops`.
- `app/mcp/tools/loopskill_catalog.py:55-78` implements `loopskill_get_loop`.
- `app/mcp/tools/__init__.py:47-51` exports the loop/personality MCP tools.
- `app/mcp/tools/__init__.py:98-102` includes the names in `__all__`.
- `app/mcp/server.py:48-52` imports the loop catalog tools.
- `app/mcp/server.py:141-150` dispatches `loopskill_search_loops` and `loopskill_get_loop`.
- `app/mcp/registry.py:282-294` includes `_loopskill_catalog_tools()` in `_tool_definitions()`.

Existing alias pattern:
- `app/mcp/_alias_map.py:1-12` documents canonical `loopskill_* -> recipes_*` normalization.
- `app/mcp/_alias_map.py:20-54` maps canonical tool names to compat dispatch names.
- `app/mcp/_alias_map.py:60-67` normalizes tool names before dispatch.
- `app/mcp/_alias_map.py:70-90` can generate compat tool entries.
- `app/mcp/_alias_map.py:73-75` explicitly says other tools, including `loopskill_search_loops`, are skipped.
- `tests/test_loopskill_mcp_tool_rename.py:169-173` pins that `loopskill_search_loops` and `loopskill_get_personality` are currently **not** aliased.

Therefore, verifier MCP aliases need new tests and a deliberate alias mapping. They will not happen automatically.

### Seeds and fixtures

- `scripts/seed_starter_catalog.py:49-50` defines `STARTER_LOOPS`.
- `scripts/seed_starter_catalog.py:52-99` defines `hello-world-loop` and documents `POST /api/loops/hello-world-loop/run`.
- `scripts/seed_starter_catalog.py:102-476` defines additional starter loops (`pr-review-loop`, `daily-briefing-loop`, `test-green-loop`, `lint-clean-loop`, `secret-scan-loop`, `changelog-from-commits-loop`, `doc-coverage-loop`, `json-schema-validate-loop`).
- `scripts/seed_starter_catalog.py:654-682` imports `Loop`, checks `Loop.slug`, and inserts `Loop` rows.
- `scripts/seed_starter_catalog.py:719-725` reports `loops_created`.

### Backend tests

- `tests/test_loop_personality_registries.py:15-21` imports loop router and MCP loop tools.
- `tests/test_loop_personality_registries.py:57-73` defines `VALID_LOOP`.
- `tests/test_loop_personality_registries.py:87-138` tests publish/get/list/404 for `/api/loops`.
- `tests/test_loop_personality_registries.py:176-205` tests MCP search/get loop.
- `tests/test_loop_runner.py:1-11` documents runner route tests for `/api/loops/{slug}/run`.
- `tests/test_loop_runner.py:26` imports `loop_router`.
- `tests/test_loop_runner.py:69-84` publishes a minimal loop through `/api/loops`.
- `tests/test_loop_runner.py:383-505` tests run auth, 404, agent mode, unknown mode, pass/fail, workspace files, master, wrong scope, private loop, require-sandbox.
- `tests/test_loop_runner.py:508-569` tests run count and ratings.
- `tests/test_loop_validation.py:10-13` imports `LoopValidationError` and `validate_loop_manifest`.
- `tests/test_loop_validation.py:35-110` validates the contract rules.
- `tests/test_p1_selfhost_smoke.py:19` imports `Loop`.
- `tests/test_p1_selfhost_smoke.py:74-76` requires at least one public loop after seeding.
- `tests/test_p1_selfhost_smoke.py:84-90` asserts seed idempotency by loop count.
- `tests/test_seed_loop_library.py:21-23` imports `LoopRunner`, validator, and `STARTER_LOOPS`.
- `tests/test_seed_loop_library.py:75-123` validates and executes seeded loop verification scripts.
- `tests/test_loopskill_mcp_tool_rename.py:169-173` currently asserts loop catalog MCP tools are not alias-normalized.

### Terminology lint

- `scripts/terminology_lint.py:24-29` bans only `cookbook`, `recipe`, and brand-sense `chef`.
- `scripts/terminology_lint.py:113-125` self-tests that "loop registry endpoint" is allowed.
- `TERMINOLOGY.md:17-22` currently defines `loop` as one of the four artifact types.
- `TERMINOLOGY.md:27-42` has no `Loop -> Verifier` mapping today.

Phase A cannot satisfy "old usage cannot creep back" without updating this lint to understand context-sensitive forbidden use of `loop` for verifier-only artifacts. A blanket `loop` ban would be too noisy because new composite `Loop` remains valid.

### Backend docs

- `README.md:3-4` markets loops as the hero object.
- `README.md:35-46` documents seeding and running `/api/loops/hello-world-loop/run`.
- `README.md:64-89` explains "Loops are the bet".
- `README.md:111-115` lists `GET/POST /api/loops` endpoints.
- `README.md:136-137` lists `loop_routes.py` and `loop_runner.py`.
- `docs/SELF_HOST.md:64` lists loops in the self-host seed surface.

### Portal public pages and fetch calls

- `src/pages/loops/index.astro:1-12` documents the public loop library and says it client-fetches `GET /api/loops`.
- `src/pages/loops/index.astro:16` sets `API_BASE` default to `https://recipes.wisechef.ai`.
- `src/pages/loops/index.astro:19-32` renders the public "Loops" page copy.
- `src/pages/loops/index.astro:41-54` renders loading/empty/error/grid states.
- `src/pages/loops/index.astro:93-107` fetches `${API_BASE}/api/loops`.
- `src/pages/loops/index.astro:120-166` renders loop cards.
- `src/pages/loops/index.astro:123` links cards to `/loops/run?slug=...`.
- `src/pages/loops/run.astro:1-14` documents public detail and run behavior.
- `src/pages/loops/run.astro:18` sets `API_BASE` default to `https://recipes.wisechef.ai`.
- `src/pages/loops/run.astro:21-27` renders the "Run this loop" page and back-link to `/loops`.
- `src/pages/loops/run.astro:43-52` renders safety contract and verification script.
- `src/pages/loops/run.astro:54-80` renders run CTA/sign-in states.
- `src/pages/loops/run.astro:151-159` fetches `${API_BASE}/api/loops/{slug}`.
- `src/pages/loops/run.astro:162-189` reads current payload fields: `title`, `description`, `run_count`, `rating_avg`, `max_turns`, `budget_usd`, `tool_allowlist`, `tier`, `license`, `latest_version`, `success_condition`, `verification_script`.
- `src/pages/loops/run.astro:213-217` posts `${API_BASE}/api/loops/{slug}/run`.
- `src/pages/loops/run.astro:278-286` posts `${API_BASE}/api/loops/{slug}/rate`.

The default `recipes.wisechef.ai` base is direct evidence that touching `/api/loops` there affects the live public portal unless deployment env overrides it.

### Portal navigation, hero, docs, and external consumer docs

- `src/layouts/AppShell.astro:47` default site description says "vetted, runnable agent loops".
- `src/layouts/AppShell.astro:77` nav item points `Loops` to `/loops`.
- `src/components/Footer.astro:15` footer links to `/loops`.
- `src/components/Footer.astro:33` links "Publish a loop" to `/creators`.
- `src/pages/index.astro:291-296` stats/pillars describe loop runs and MCP-native loop execution.
- `src/pages/index.astro:330-343` hero links to `/loops` and says "Run a loop. Get a verdict."
- `src/pages/index.astro:521` says the meta-skill can run vetted loops.
- `src/pages/index.astro:726-731` says creators can publish a vetted runnable loop.
- `src/pages/pricing.astro:21` includes "Browse + run every public loop".
- `src/pages/docs/mcp.astro:10-25` describes MCP tools; current examples are still `recipes_search`.
- `src/pages/docs/mcp.astro:159` lists `recipes_search`.
- `src/pages/docs/new-agent.astro:31-65` mentions `recipes_search`, `recipes_install`, `recipes_sync`, and "all other LoopSkill tools".
- `src/pages/llms.txt.ts:73-75` fallback MCP list starts with `recipes_search`; `src/pages/llms.txt.ts:164-165` emits the MCP tool list from snapshot/fallback.

## 2. DB table rename: required or not?

**Recommendation: do not rename the physical DB tables in Phase A. Keep `loops`, `loop_versions`, and `loop_ratings` as storage tables. Rename at the ORM/API/tool semantic layer only.**

Reasons:

- The existing DB graph has multiple FK-dependent objects: `loop_ratings.loop_id -> loops.id` at `app/models.py:1360-1362`, `loop_versions.loop_id -> loops.id` at `app/models.py:1375`, migration creation at `loopskill_0622_p8_runnable_types.py:63-77`, and rating migration at `lsk0627_loop_feedback.py:60-99`.
- Alembic is startup-enforced: `app/main.py:116-120` runs `check_alembic_heads()`. A risky rename requires a migration that must be correct before systemd restart succeeds.
- Prod is Postgres and zero-downtime deploy is a systemd restart. A table rename would require coordinating app code and migration timing. During restart, any code still looking at `loops` would fail if the table has been renamed; any new code looking at `verifiers` would fail before migration.
- The prior cookbook->bundle migration was data-preserving but high-touch: `f1b2c3d4e5a6_p34_rename_cookbook_to_bundle.py:7-33` documents table and column renames and idempotency. That was warranted for a mature domain rename. Here the shipped object is only weeks old, and the new composite `Loop` will likely need a new table shape anyway.
- Keeping `loops` as an internal storage name is compatible with an ORM class named `Verifier` via `__tablename__ = "loops"` and class aliases for compatibility.

If a physical rename is ever desired, it should be a later dedicated migration with dual-compatible views or a carefully staged deploy. It is not required for Phase A.

## 3. Safest alias strategy

### HTTP API

Use **dual-mount/proxy behavior**, not 301, for API routes:

- Add canonical verifier routes such as `/api/verifiers`, `/api/verifiers/{slug}`, `/api/verifiers/{slug}/run`, `/api/verifiers/{slug}/rate`.
- Keep `/api/loops` serving the old verifier payload unchanged for at least one release window.
- Do not 301 API clients from `/api/loops` to `/api/verifiers`; non-browser clients, `fetch`, POST bodies, auth cookies, and MCP/docs consumers should not be forced through redirect behavior.
- Mark `/api/loops` responses with compatibility metadata/header if desired, but do not change status codes.
- Add `/api/verifiers` to public prefixes with tests mirroring `tests/test_loopskill_portal_0627_public_loop_browse.py:35-121`.

Rationale: the current portal fetches `/api/loops` anonymously, and writes depend on route-level guards because the prefix is method-agnostic public.

### Python/module aliases

Safest code shape:

- New module `verifier_routes.py` owns canonical implementation.
- `loop_routes.py` stays as a compatibility shim or dual-router export for old imports.
- New ORM class `Verifier` maps to `__tablename__ = "loops"`.
- Keep `Loop = Verifier` alias for tests/import consumers through one release window.
- Rename schemas to `VerifierOut`, etc., but keep `LoopOut = VerifierOut` aliases if existing imports remain.
- Rename validation/runner modules only if aliases are kept: `loop_validation.py` and `loop_runner.py` are imported directly by tests and probably downstream users.

### MCP aliases

The existing alias layer can be reused, but it must be extended deliberately:

- Today `normalize_tool_name()` maps canonical loopskill tool names to legacy recipes dispatch names at `app/mcp/_alias_map.py:60-67`.
- Registry compatibility aliases are generated from the alias map at `app/mcp/registry.py:296-305`.
- However, `loopskill_search_loops` and `loopskill_get_loop` are explicitly not aliased today: `app/mcp/_alias_map.py:73-75`, and tests assert that at `tests/test_loopskill_mcp_tool_rename.py:169-173`.

For Phase A:

- Canonical new names should be `loopskill_search_verifiers` and `loopskill_get_verifier`.
- Old names `loopskill_search_loops` and `loopskill_get_loop` must remain advertised and callable as aliases.
- Normalize old-to-new or new-to-old at the top of dispatch, but pick one internal dispatch name and keep a single implementation.
- Add tests that both old and new names appear in `_tool_definitions()` and both dispatch to the same result.

### Portal URL strategy

Do **not** redirect `/loops` in Phase A.

Keep `/loops` as the public page for the shipped verifier catalog until the new composite Loop is ready. Options:

- Rename visible copy to "Verifiers" while keeping URL `/loops` for compatibility, or
- Add `/verifiers` as a canonical page and keep `/loops` as an alias page rendering the same component.

Avoid a browser 301 from `/loops` to `/verifiers` in Phase A because:

- The homepage and nav already link `/loops` (`AppShell.astro:77`, `Footer.astro:15`, `index.astro:330-343`).
- Search/indexed public URLs may already exist.
- The new composite `Loop` will likely want `/loops` later, so burning a redirect now creates a second migration problem.

Best path:

- Phase A: `/loops` remains live and still shows verifier artifacts.
- Add `/verifiers` optionally.
- Phase B/C: introduce composite loops under a new preview URL first, e.g. `/composite-loops` or `/loops-next`, or API `/api/composite-loops`.
- Later: move `/loops` to composite only after old `/loops` verifier traffic has an explicit `/verifiers` home and old API/MCP aliases remain stable.

## 4. Naming collision: can new composite `Loop` take the old name in one phase?

**No. A same-name handover in one phase is not safe.**

Evidence:

- `/api/loops` currently means "verifier object" for public browse/detail/run/rate (`app/loop_routes.py:39-361`).
- `/loops` currently renders those fields and posts to those endpoints (`src/pages/loops/index.astro:93-107`, `src/pages/loops/run.astro:151-286`).
- MCP `loopskill_search_loops` and `loopskill_get_loop` currently return the verifier contract (`app/mcp/tools/loopskill_catalog.py:18-78`).
- Tests assert this object’s behavior and public accessibility across route, runner, MCP, seed, and portal-public middleware.

If the new composite `Loop` claims `/api/loops` or `loopskill_get_loop` in the same PR, old clients will receive a different object shape. The portal detail page expects `verification_script`, `success_condition`, `max_turns`, `tool_allowlist`, `run_count`, `rating_*`; a composite payload with `{cron/heartbeat, skills, subagents_config, connectors, verifier, state_seed}` will break rendering or semantics.

Recommended sequencing:

1. **PR A1: verifier rename and aliases only.**
   - Storage remains `loops`.
   - `/api/loops` still returns verifier payload.
   - `/api/verifiers` is added.
   - MCP new verifier names are added; old loop names stay aliases.
   - Portal `/loops` remains 200.
2. **PR A2/B: add composite Loop under new non-conflicting surfaces.**
   - New table(s), e.g. `composite_loops` or `loops_v2`, not the existing `loops`.
   - New API path, e.g. `/api/composite-loops` or `/api/loop-instances`.
   - New MCP names, e.g. `loopskill_create_loop`, `loopskill_get_composite_loop`, with no reuse of old get/search loop names yet.
3. **PR C: public name handover.**
   - Portal `/loops` becomes composite after `/verifiers` exists and is linked.
   - Old `/api/loops` verifier aliases get deprecation headers, not removal.
   - MCP old names remain stable or are versioned.

## 5. Kill-tests required in the PR

Backend API:

- `GET /api/loops` still returns 200 anonymously and returns verifier rows.
- `GET /api/loops/{slug}` still returns 200 anonymously and includes `success_condition`, `verification_script`, `max_turns`, `tool_allowlist`, `run_count`, `rating_count`.
- `POST /api/loops/{slug}/run` still requires auth and still runs verifier mode.
- `POST /api/loops/{slug}/rate` still requires auth and updates rating aggregate.
- `POST /api/loops` still requires auth or is intentionally deprecated with a compatibility test; do not silently change behavior.
- `GET /api/verifiers` returns the exact same rows and response shape as `/api/loops`.
- `GET /api/verifiers/{slug}` returns the exact same payload as `/api/loops/{slug}`.
- `POST /api/verifiers/{slug}/run` and `/rate` match old behavior.
- Anonymous `/api/verifiers/{slug}/run`, `/rate`, and `POST /api/verifiers` are 401, mirroring the public-prefix self-guard tests.
- Private verifier run remains 404 for non-owner and 200 for master, mirroring `tests/test_loop_runner.py:460-494`.
- `WR_LOOP_RUN_REQUIRE_SANDBOX=true` still returns 503 when no sandbox is functional, mirroring `tests/test_loop_runner.py:496-505`.

MCP:

- `loopskill_search_verifiers` is advertised.
- `loopskill_get_verifier` is advertised.
- `loopskill_search_loops` is still advertised.
- `loopskill_get_loop` is still advertised.
- `normalize_tool_name()` or equivalent maps old/new names to one implementation.
- `call_tool_sync("loopskill_get_loop", ...)` and `call_tool_sync("loopskill_get_verifier", ...)` both resolve the same fixture.
- Old tool name still resolves, not just appears in `tools/list`.

Portal:

- `/loops` page builds and loads without changing URL.
- `/loops/run?slug=hello-world-loop` builds and still references a working old API path or a tested alias.
- Anonymous portal user sees no 404 and no auth wall on browse/detail.
- Signed-in/browser-cookie path can still run and rate via old `/api/loops` or tested alias.
- If `/verifiers` is added, it renders the same component and fetches `/api/verifiers`.

DB/Alembic:

- `alembic heads` remains single-head.
- Upgrade from current prod head succeeds on Postgres.
- Cold SQLite/self-host seed still creates verifier rows through the old storage table.
- `loop_versions` and `loop_ratings` remain queryable and tied to the renamed ORM class.
- Existing `reconcile_events` history remains queryable; add a regression that inserting/querying `ReconcileEvent` still works after migrations.

Seeds:

- Starter catalog still seeds the old rows, now semantically verifiers.
- Seed idempotency still works by slug.
- Seeded verifier contracts still pass validation.
- Seeded verification scripts still pass/fail non-vacuously.

Terminology lint:

- New linter rule catches new verifier-only code using `Loop`/`loop` except in alias shims/tests/docs marked compat.
- New linter permits composite `Loop` usage only in the new composite modules/docs.
- Self-tests cover allowed composite usage, forbidden verifier usage, and compat alias markers.

Docs/external consumers:

- README/API docs explain `/api/loops` as compatibility for verifiers and `/api/verifiers` as canonical.
- MCP docs list new verifier tool names and old aliases.
- `llms.txt` generated/fallback tool list includes canonical verifier tools and does not drop old aliases abruptly.
- No changes to `recipes.wisechef.ai` behavior unless explicitly staged; portal defaults still hit that host in `src/pages/loops/index.astro:16` and `src/pages/loops/run.astro:18`.

## 6. Conditions for GO

Required conditions:

1. **No physical DB table rename in Phase A.** Keep `loops`, `loop_versions`, `loop_ratings`.
2. **No same-phase semantic reuse of `/api/loops`, `/loops`, `loopskill_search_loops`, or `loopskill_get_loop` for composite loops.**
3. **Dual API surfaces, not API redirects.** `/api/loops` remains old verifier behavior; `/api/verifiers` is added.
4. **MCP dual names.** New verifier names are canonical, old loop names remain advertised and callable.
5. **Portal `/loops` remains 200 and anonymous-readable.** Optional `/verifiers` can be added, but `/loops` must not disappear.
6. **Terminology lint is context-aware.** It must prevent old verifier-as-loop terminology in new verifier code without blocking the future composite `Loop`.
7. **Full kill-test suite above is included.**
8. **Composite `Loop` ships separately.** It gets separate tables and surfaces first; public name handover is later.

Final recommendation:

**GO-WITH-CONDITIONS** for a Phase A PR scoped to verifier rename + compatibility aliases. **NO-GO** for a single PR that both renames the old object and lets the new composite artifact claim the old public/API/MCP loop names.
